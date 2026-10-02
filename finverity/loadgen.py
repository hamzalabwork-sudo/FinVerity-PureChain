"""One experimental trial: workload -> evidence -> commitment -> submission -> verification.

Two commitment modes:
  * per-record (batch_size = 1): one recordEvidence transaction per evidence package;
  * batched (batch_size = k): records are grouped as they arrive - a batch closes when it has
    k records or when its first record has waited batch_wait_s - and one recordBatch
    transaction commits the Merkle root of the group. Each record keeps an inclusion proof.

Record latency is measured end-to-end from the record's arrival (its scheduled time), so the
time spent waiting for a batch to fill is always included.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import accounts, datasets, evidence, merkle
from .backends.base import Backend, Item
from .canonical import sha256
from .config import COST, ENERGY, ETH_USD, MAINNET_GAS_GWEI, RESULTS
from .resources import ResourceMonitor, remote_validator_energy_j
from .storage import EvidenceStore

CONF_DEPTHS = (1, 2, 3, 6)


@dataclass
class TrialConfig:
    backend: str
    source: str = "momtsim"
    n: int = 1000
    level: int = 4
    doc_bytes: int = 100 * 1024
    rate: float = 50.0            # offered load, records/s (0 = unthrottled)
    arrival: str = "poisson"      # poisson | constant
    workers: int = 16
    seed: int = 1
    trial: int = 0
    workload: str = "adhoc"
    verify_n: int = 1000          # records verified by the auditor after the run
    wait_timeout_s: float = 300.0
    batch_size: int = 1
    batch_wait_s: float = 1.0
    keep_store: bool = False
    tag: str = ""

    @property
    def run_id(self) -> str:
        b = f"_b{self.batch_size}w{self.batch_wait_s:g}" if self.batch_size > 1 else ""
        return (f"{self.workload}_{self.backend}_{self.source}_n{self.n}_L{self.level}_d{self.doc_bytes}"
                f"_r{self.rate:g}{b}_s{self.seed}_t{self.trial}{('_' + self.tag) if self.tag else ''}")


def _pct(xs, q):
    return float(np.percentile(xs, q)) if len(xs) else float("nan")


def _stats(prefix: str, xs) -> dict:
    xs = [x for x in xs if x == x]
    if not xs:
        return {f"{prefix}_{k}": float("nan") for k in ("mean", "sd", "median", "p95", "p99", "max")}
    return {f"{prefix}_mean": float(np.mean(xs)), f"{prefix}_sd": float(np.std(xs, ddof=1)) if len(xs) > 1 else 0.0,
            f"{prefix}_median": float(np.median(xs)), f"{prefix}_p95": _pct(xs, 95),
            f"{prefix}_p99": _pct(xs, 99), f"{prefix}_max": float(np.max(xs))}


def arrivals(n: int, rate: float, kind: str, seed: int) -> np.ndarray:
    if rate <= 0:
        return np.zeros(n)
    if kind == "constant":
        return np.arange(n) / rate
    gaps = np.random.default_rng(seed + 7919).exponential(1.0 / rate, n)
    return np.concatenate(([0.0], np.cumsum(gaps[:-1])))


def form_batches(sched: np.ndarray, k: int, wait_s: float) -> list[tuple[list[int], float]]:
    """Group records in arrival order; a batch closes at the k-th arrival or first arrival + wait_s."""
    out, cur, first = [], [], 0.0
    for i, t in enumerate(sched):
        if cur and t > first + wait_s:
            out.append((cur, first + wait_s))
            cur = []
        if not cur:
            first = t
        cur.append(i)
        if len(cur) == k:
            out.append((cur, t))
            cur = []
    if cur:
        out.append((cur, first + wait_s))
    return out


def commitment_checker(backend: Backend, store: EvidenceStore):
    """Return check(record_id, recomputed_hash) -> (matches_commitment, on_chain_record_or_batch)."""
    def check(record_id: bytes, h: bytes):
        pr = store.get_proof(record_id)
        if pr is None:
            rec = backend.fetch_record(record_id)
            return (rec is not None and rec["evidence_hash"] == h), rec
        batch_id, proof = pr
        rec = backend.fetch_batch(batch_id)
        return (rec is not None and merkle.verify(merkle.leaf(record_id, h), proof, rec["merkle_root"])), rec
    return check


def run_trial(cfg: TrialConfig, backend: Backend, log=print) -> dict:
    out_dir = RESULTS / "raw" / cfg.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    rs = accounts.roles(n_submitters=cfg.workers)
    approver_addrs = [a.address for a in rs.approvers]
    batching = cfg.batch_size > 1

    log(f"[{cfg.run_id}] setup")
    setup_info = backend.setup(rs.submitters, rs.approvers)

    # ---------------------------------------------------------- preparation (client side)
    txs = datasets.load_workload(cfg.source, cfg.n, seed=cfg.seed * 1000 + cfg.trial)
    sched = arrivals(len(txs), cfg.rate, cfg.arrival, cfg.seed * 1000 + cfg.trial)
    groups = form_batches(sched, cfg.batch_size, cfg.batch_wait_s) if batching else None
    batch_approver = {}
    if batching:
        for j, (idxs, _) in enumerate(groups):
            for i in idxs:
                batch_approver[i] = j % len(approver_addrs)

    store = EvidenceStore(cfg.run_id)
    salt = os.urandom(8).hex()   # makes record IDs unique if a trial is re-executed
    items: list[Item] = []
    t_prep0 = time.perf_counter()
    for i, tx in enumerate(txs):
        t0 = time.perf_counter()
        b = evidence.generate(tx, cfg.level, cfg.doc_bytes, seed=cfg.seed, approver_addresses=approver_addrs,
                              run_id=cfg.run_id, approver_index=batch_approver.get(i))
        t1 = time.perf_counter()
        record_id = sha256(f"{cfg.run_id}|{salt}|{tx['tx_id']}".encode())
        store.put(record_id, b)
        t2 = time.perf_counter()
        it = Item(idx=i, tx_id=tx["tx_id"], record_id=record_id, evidence_hash=b.evidence_hash,
                  meta_hash=b.meta_hash, level=cfg.level, approver_index=b.approver_index,
                  submitter_index=i % cfg.workers, evidence_bytes=b.size_bytes)
        it.prep["evidence_gen_hash_s"] = t1 - t0
        it.prep["offchain_store_s"] = t2 - t1
        items.append(it)

    if batching:
        units, unit_sched = [], []
        for j, (idxs, close_t) in enumerate(groups):
            members = [items[i] for i in idxs]
            t0 = time.perf_counter()
            root, proofs = merkle.build([merkle.leaf(m.record_id, m.evidence_hash) for m in members])
            batch_id = sha256(f"{cfg.run_id}|{salt}|batch|{j}".encode())
            store.put_proofs([(m.record_id, batch_id, pr) for m, pr in zip(members, proofs)])
            dt = time.perf_counter() - t0
            for m in members:
                m.prep["merkle_s"] = dt / len(members)
            u = Item(idx=j, tx_id=f"BATCH-{j}", record_id=batch_id, evidence_hash=root,
                     meta_hash=sha256(b"".join(m.meta_hash for m in members)), level=cfg.level,
                     approver_index=batch_approver[idxs[0]], submitter_index=j % cfg.workers,
                     evidence_bytes=sum(m.evidence_bytes for m in members), members=members)
            units.append(u)
            unit_sched.append(close_t)
        unit_sched = np.array(unit_sched)
    else:
        units, unit_sched = items, sched

    if units and hasattr(backend, "calibrate_gas"):
        u0 = units[0]
        est = backend.calibrate_gas(u0, rs.submitters[0], rs.approvers[u0.approver_index])
        log(f"[{cfg.run_id}] gas estimate {'recordBatch' if batching else 'recordEvidence'} = {est}")
    for u in units:
        backend.prepare(u, rs.submitters[u.submitter_index], rs.approvers[u.approver_index])
    prep_s = time.perf_counter() - t_prep0
    log(f"[{cfg.run_id}] prepared {len(items)} records in {len(units)} submissions in {prep_s:.1f}s")

    # ---------------------------------------------------------- submission
    per_worker: list[list[Item]] = [[] for _ in range(cfg.workers)]
    for u in units:
        per_worker[u.submitter_index % cfg.workers].append(u)

    mon = ResourceMonitor(backend.containers)
    backend.start(cfg.workers)
    time.sleep(0.3)
    mon.start()
    t_start = time.perf_counter() + 0.2

    def worker(w: int):
        for u in per_worker[w]:
            u.t_sched = t_start + unit_sched[u.idx]
            delay = u.t_sched - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            backend.send(u, w)

    threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(cfg.workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t_sent = time.perf_counter()
    backend.wait(units, cfg.wait_timeout_s)
    res = mon.stop()
    backend.stop()
    log(f"[{cfg.run_id}] submission phase {t_sent - t_start:.1f}s, busy {res['elapsed_s']:.1f}s")
    reorg = backend.reorg_check() if hasattr(backend, "reorg_check") else {}

    # propagate batch outcomes to member records; every record's arrival is its own schedule
    if batching:
        for u in units:
            k = len(u.members)
            for m in u.members:
                for f in ("t_send", "t_ack", "t_commit", "t_first_seen", "t_final", "status", "error", "block",
                          "block_hash", "tx_hash", "orphaned_sightings"):
                    setattr(m, f, getattr(u, f))
                m.conf = dict(u.conf)
                m.gas_used = u.gas_used / k if u.gas_used else 0
                m.payload_bytes = u.payload_bytes / k
    for it in items:
        it.t_sched = t_start + sched[it.idx]

    # ---------------------------------------------------------- continuous verification (auditor)
    check = commitment_checker(backend, store)
    committed = [it for it in items if it.status == "committed"]
    rng = np.random.default_rng(cfg.seed + cfg.trial)
    sample = [committed[i] for i in sorted(rng.choice(len(committed), min(cfg.verify_n, len(committed)),
                                                      replace=False))] if committed else []

    def verify(it: Item) -> dict:
        t0 = time.perf_counter()
        pkg, docs = store.get(it.record_id)
        t1 = time.perf_counter()
        h = evidence.recompute(pkg, docs)
        t2 = time.perf_counter()
        match, rec = check(it.record_id, h)
        t3 = time.perf_counter()
        binding = (match and pkg["record"]["tx_id"] == it.tx_id and rec["approver"] == pkg["attestation"]["approver"])
        return {"idx": it.idx, "chain_fetch_s": t3 - t2, "store_fetch_s": t1 - t0, "rehash_s": t2 - t1,
                "total_s": t3 - t0, "match": bool(match), "binding": bool(binding)}

    tv0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(8, cfg.workers)) as ex:
        vrows = list(ex.map(verify, sample))
    verify_wall = time.perf_counter() - tv0

    # ---------------------------------------------------------- persist raw data
    rows = []
    for it in items:
        d = {k: v for k, v in asdict(it).items() if k not in ("payload", "approval", "prep", "record_id",
                                                              "evidence_hash", "meta_hash", "conf", "members")}
        d.update({"record_id": it.record_id.hex(), "evidence_hash": it.evidence_hash.hex(), **it.prep})
        if backend.kind != "blockchain":            # durable commit is final and immediately visible
            d["t_first_seen"] = it.t_commit
            d["t_final"] = it.t_commit
        for k in CONF_DEPTHS:
            d[f"t_conf{k}"] = it.conf.get(k, it.t_commit if backend.kind != "blockchain" else float("nan"))
        for k in ("t_sched", "t_send", "t_ack", "t_commit", "t_first_seen", "t_final",
                  *[f"t_conf{k}" for k in CONF_DEPTHS]):
            d[k] = d[k] - t_start if d[k] else float("nan")
        rows.append(d)
    df = pd.DataFrame(rows)
    df["ack_latency_s"] = df["t_ack"] - df["t_send"]
    df["commit_latency_s"] = df["t_commit"] - df["t_send"]        # submission -> canonical inclusion
    df["e2e_latency_s"] = df["t_commit"] - df["t_sched"]          # record arrival -> canonical inclusion
    df["queue_delay_s"] = df["t_send"] - df["t_sched"]            # client queueing + batch fill wait
    df["first_seen_latency_s"] = df["t_first_seen"] - df["t_send"]
    df["final_latency_s"] = df["t_final"] - df["t_send"]         # finality under the backend's rule
    df["final_e2e_latency_s"] = df["t_final"] - df["t_sched"]
    for k in CONF_DEPTHS:
        df[f"conf{k}_latency_s"] = df[f"t_conf{k}"] - df["t_send"]
        df[f"conf{k}_e2e_latency_s"] = df[f"t_conf{k}"] - df["t_sched"]
    df.to_parquet(out_dir / "items.parquet", index=False)
    blocks = backend.block_log()
    for b in blocks:
        b["t_observed"] -= t_start
    pd.DataFrame(blocks).to_csv(out_dir / "blocks.csv", index=False)
    cblocks = [b for b in blocks if b.get("canonical", True)]
    vdf = pd.DataFrame(vrows)
    vdf.to_parquet(out_dir / "verify.parquet", index=False)

    # ---------------------------------------------------------- summary
    ok = df[df["status"] == "committed"]
    n_ok = len(ok)
    if n_ok:
        span = ok["t_commit"].max() - df["t_sched"].min()
        throughput = n_ok / span if span > 0 else float("nan")
    else:
        span, throughput = float("nan"), 0.0
    win = mon.window(t_start, float(np.nanmax([it.t_commit for it in items if it.status == "committed"]))
                     if n_ok else t_start + 1e-9)
    busy_s = win["window_s"]                       # processing window: first arrival -> last inclusion
    # Two energy accountings (both reported for every backend):
    #  (1) attributed: every infrastructure node draws P_node for the busy period,
    #      E = n_nodes * P_node * T + E_client  (comparable across backends)
    #  (2) measured dynamic: CPU-time based energy of client + local containers (not observable
    #      for the remote PureChain validators, reported as client-only there).
    n_nodes = backend.n_validators if backend.kind == "blockchain" else 1
    attributed_j = remote_validator_energy_j(n_nodes, busy_s) + win["window_client_energy_j"]
    dynamic_j = win["window_client_energy_j"] + win["window_infra_energy_j"]
    evid_bytes = int(df["evidence_bytes"].sum())
    onchain_bytes = float(df["payload_bytes"].sum())
    extra = backend.extra_metrics(units)
    net_bytes = extra.get("rpc_bytes_out", 0) + extra.get("rpc_bytes_in", 0) or res.get("infra_net_bytes", 0)
    replicas = max(backend.n_validators, 1)
    c_compute = attributed_j / 3.6e6 * COST.electricity_usd_per_kwh
    c_storage = (evid_bytes / 1e9 * COST.storage_usd_per_gb_month
                 + onchain_bytes * replicas / 1e9 * COST.chain_storage_usd_per_gb_month) * COST.retention_months
    c_network = net_bytes / 1e9 * COST.network_usd_per_gb
    gas_price = getattr(backend, "gas_price", 0) or 0
    gas_total = float(ok["gas_used"].sum())
    fee_devnet_eth = gas_total * gas_price / 1e18
    # fees are charged only where the network prices gas (PoS baseline); PureChain / Besu: gas price 0
    fee_bearing = getattr(getattr(backend, "chain", None), "fee_bearing", False)
    c_fee = gas_total * MAINNET_GAS_GWEI * 1e-9 * ETH_USD if fee_bearing else 0.0
    per = (lambda x: x / n_ok) if n_ok else (lambda x: float("nan"))

    summary = {
        **asdict(cfg), "run_id": cfg.run_id, "salt": salt, **backend.info(), **setup_info,
        "submissions": len(units), "records_per_submission": len(items) / max(len(units), 1),
        "submitted": len(df), "committed": n_ok, "failed": int((df["status"] == "failed").sum()),
        "timeout": int((df["status"] == "timeout").sum()), "reverted": int((df["status"] == "reverted").sum()),
        "success_rate": n_ok / len(df) if len(df) else float("nan"),
        "offered_rate": cfg.rate, "achieved_send_rate": len(units) / (t_sent - t_start) if t_sent > t_start else float("nan"),
        "throughput_tps": throughput, "commit_span_s": span,
        **_stats("commit_latency_s", ok["commit_latency_s"]),
        **_stats("e2e_latency_s", ok["e2e_latency_s"]),
        **_stats("final_latency_s", ok["final_latency_s"]),
        **_stats("final_e2e_latency_s", ok["final_e2e_latency_s"]),
        "fee_devnet_eth_per_tx": per(fee_devnet_eth),
        **_stats("ack_latency_s", ok["ack_latency_s"]),
        **_stats("first_seen_latency_s", ok["first_seen_latency_s"]),
        **{kk: vv for k in CONF_DEPTHS for kk, vv in _stats(f"conf{k}_latency_s", ok[f"conf{k}_latency_s"]).items()},
        **{kk: vv for k in CONF_DEPTHS for kk, vv in _stats(f"conf{k}_e2e_latency_s", ok[f"conf{k}_e2e_latency_s"]).items()},
        "commit_time_unobserved": int(ok["commit_latency_s"].isna().sum()),
        "txs_orphaned_first": int((ok["orphaned_sightings"] > 0).sum()),
        **_stats("queue_delay_s", df["queue_delay_s"]),
        "blocks": len(cblocks),
        "txs_per_block_mean": float(np.mean([b["tx_count"] for b in cblocks if b["tx_count"]])) if any(b["tx_count"] for b in cblocks) else 0.0,
        "txs_per_block_max": max([b["tx_count"] for b in cblocks], default=0),
        "block_signers": len({b["signer"] for b in cblocks if b.get("signer")}),
        "gas_used_mean": float(ok["gas_used"].mean()) if n_ok else float("nan"),
        "prep_total_s": prep_s,
        **_stats("prep_evidence_s", df["evidence_gen_hash_s"]),
        **_stats("prep_store_s", df["offchain_store_s"]),
        "evidence_bytes_total": evid_bytes, "evidence_bytes_mean": evid_bytes / max(len(df), 1),
        "onchain_payload_bytes_mean": onchain_bytes / max(len(df), 1),
        "offchain_store_bytes": store.size_bytes(),
        **{k: v for k, v in res.items() if k != "infra_containers"}, **win,
        "energy_nodes": n_nodes, "energy_attributed_j": attributed_j, "energy_j_per_tx": per(attributed_j),
        "energy_dynamic_j": dynamic_j, "infra_energy_measured": bool(backend.containers),
        "energy_dynamic_j_per_tx": per(dynamic_j),
        "co2_g_per_tx": per(attributed_j / 3.6e6 * ENERGY.grid_gco2_per_kwh),
        "network_bytes": net_bytes, **extra,
        "cost_compute_usd_per_tx": per(c_compute), "cost_storage_usd_per_tx": per(c_storage),
        "cost_network_usd_per_tx": per(c_network), "cost_fee_usd_per_tx": per(c_fee),
        "cost_total_usd_per_tx": per(c_compute + c_storage + c_network + c_fee),
        **reorg,
        "verified": len(vdf),
        "verify_success_rate": float(vdf["match"].mean()) if len(vdf) else float("nan"),
        "binding_success_rate": float(vdf["binding"].mean()) if len(vdf) else float("nan"),
        **_stats("verify_total_s", vdf["total_s"] if len(vdf) else []),
        **_stats("verify_chain_s", vdf["chain_fetch_s"] if len(vdf) else []),
        **_stats("verify_rehash_s", vdf["rehash_s"] if len(vdf) else []),
        "verify_throughput_per_s": len(vdf) / verify_wall if len(vdf) and verify_wall > 0 else float("nan"),
        "errors_top": df.loc[df["error"] != "", "error"].value_counts().head(3).to_dict(),
        "infra_containers": res.get("infra_containers", {}),
    }
    if "approval_sign_s" in df:
        summary.update(_stats("prep_sign_s", (df["approval_sign_s"].fillna(0) + df["tx_build_sign_s"].fillna(0))))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    if not cfg.keep_store and not os.environ.get("FINVERITY_KEEP_STORE"):
        store.delete()
    else:
        store.close()
    log(f"[{cfg.run_id}] committed {n_ok}/{len(df)} | {throughput:.1f} rec/s | e2e median "
        f"{summary['e2e_latency_s_median']:.3f}s p95 {summary['e2e_latency_s_p95']:.3f}s | "
        f"final median {summary['final_e2e_latency_s_median']:.3f}s | verify {summary['verify_success_rate']}")
    return summary
