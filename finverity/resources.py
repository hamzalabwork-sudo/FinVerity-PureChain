"""Resource and energy metering.

* Client process: CPU time and RSS via psutil.
* Local infrastructure (Besu validators, PostgreSQL): per-container CPU, memory, network
  and block I/O via a streaming `docker stats` reader.
* Energy: linear power model (see config.EnergyModel); remote PureChain validators are
  modeled because their hardware cannot be metered from the client.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time

import psutil

from .config import ENERGY

_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def _bytes(s: str) -> float:
    m = re.match(r"([\d.]+)\s*([a-zA-Z]+)", s.strip())
    return float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1) if m else 0.0


class DockerStats(threading.Thread):
    def __init__(self, containers: list[str]):
        super().__init__(daemon=True)
        self.containers = containers
        self.samples: list[dict] = []
        self.proc = None

    def run(self):
        if not self.containers:
            return
        self.proc = subprocess.Popen(["docker", "stats", "--format", "{{json .}}", *self.containers],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in self.proc.stdout:
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
                net_in, net_out = (d["NetIO"].split("/") + ["0B"])[:2]
                blk_r, blk_w = (d["BlockIO"].split("/") + ["0B"])[:2]
                self.samples.append({
                    "t": time.perf_counter(), "name": d["Name"],
                    "cpu_pct": float(d["CPUPerc"].rstrip("%") or 0),
                    "mem_bytes": _bytes(d["MemUsage"].split("/")[0]),
                    "net_in": _bytes(net_in), "net_out": _bytes(net_out),
                    "blk_read": _bytes(blk_r), "blk_write": _bytes(blk_w),
                })
            except (KeyError, ValueError):
                continue

    def stop(self):
        if self.proc:
            self.proc.terminate()


class ResourceMonitor:
    def __init__(self, containers: list[str], interval_s: float = 0.5):
        self.proc = psutil.Process()
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self.docker = DockerStats(containers)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        self.proc.cpu_percent(None)
        psutil.cpu_percent(None)
        while not self._stop.wait(self.interval_s):
            self.samples.append({"t": time.perf_counter(), "proc_cpu_pct": self.proc.cpu_percent(None),
                                 "proc_rss": self.proc.memory_info().rss, "host_cpu_pct": psutil.cpu_percent(None)})

    def start(self):
        self.t0 = time.perf_counter()
        self.cpu0 = self.proc.cpu_times()
        self.net0 = psutil.net_io_counters()
        self.docker.start()
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=2)
        self.docker.stop()
        elapsed = time.perf_counter() - self.t0
        cpu1 = self.proc.cpu_times()
        net1 = psutil.net_io_counters()
        client_cpu_s = (cpu1.user - self.cpu0.user) + (cpu1.system - self.cpu0.system)
        out = {
            "elapsed_s": elapsed,
            "client_cpu_s": client_cpu_s,
            "client_cpu_pct_mean": _mean([s["proc_cpu_pct"] for s in self.samples]),
            "client_rss_peak_mb": max([s["proc_rss"] for s in self.samples] or [self.proc.memory_info().rss]) / 1e6,
            "host_cpu_pct_mean": _mean([s["host_cpu_pct"] for s in self.samples]),
            "host_net_sent_bytes": net1.bytes_sent - self.net0.bytes_sent,
            "host_net_recv_bytes": net1.bytes_recv - self.net0.bytes_recv,
            "client_energy_j": energy_j(client_cpu_s),
        }
        out.update(self._docker_summary())
        return out

    def window(self, t0: float, t1: float) -> dict:
        """Resource use inside the processing window [t0, t1] (first arrival -> last inclusion).

        Energy is attributed only to this window, so waiting for confirmations/finality after the
        last inclusion is not charged to any backend.
        """
        dt = self.interval_s
        client_cpu = sum(s["proc_cpu_pct"] / 100 * dt for s in self.samples if t0 <= s["t"] <= t1)
        infra_cpu = 0.0
        by = {}
        for s in self.docker.samples:
            if t0 <= s["t"] <= t1:
                by.setdefault(s["name"], []).append(s)
        for ss in by.values():
            ss.sort(key=lambda s: s["t"])
            infra_cpu += sum(a["cpu_pct"] / 100 * (b["t"] - a["t"]) for a, b in zip(ss, ss[1:]))
        return {"window_s": t1 - t0, "window_client_cpu_s": client_cpu, "window_infra_cpu_s": infra_cpu,
                "window_client_energy_j": energy_j(client_cpu), "window_infra_energy_j": energy_j(infra_cpu)}

    def _docker_summary(self) -> dict:
        by = {}
        for s in self.docker.samples:
            by.setdefault(s["name"], []).append(s)
        if not by:
            return {"infra_cpu_s": 0.0, "infra_energy_j": 0.0}
        cpu_s = mem_peak = net = blk_w = 0.0
        per = {}
        for name, ss in by.items():
            ss.sort(key=lambda s: s["t"])
            c = sum(a["cpu_pct"] / 100 * (b["t"] - a["t"]) for a, b in zip(ss, ss[1:]))
            m = max(s["mem_bytes"] for s in ss)
            n = (ss[-1]["net_in"] + ss[-1]["net_out"]) - (ss[0]["net_in"] + ss[0]["net_out"])
            w = ss[-1]["blk_write"] - ss[0]["blk_write"]
            per[name] = {"cpu_s": c, "mem_peak_mb": m / 1e6, "net_bytes": n, "blk_write_bytes": w}
            cpu_s, mem_peak, net, blk_w = cpu_s + c, mem_peak + m, net + n, blk_w + w
        return {"infra_cpu_s": cpu_s, "infra_mem_peak_mb": mem_peak / 1e6, "infra_net_bytes": net,
                "infra_blk_write_bytes": blk_w, "infra_energy_j": energy_j(cpu_s), "infra_containers": per}


def energy_j(cpu_seconds: float) -> float:
    return ENERGY.p_dynamic_w * cpu_seconds / ENERGY.logical_cores


def remote_validator_energy_j(n_validators: int, seconds: float) -> float:
    """Attributed energy of remote validators over the busy period (modeled, not measured)."""
    return n_validators * ENERGY.validator_power_w * seconds


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0
