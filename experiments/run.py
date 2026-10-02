"""FinVerity experiment CLI.

    python -m experiments.run probe                      # network facts (validators, block time)
    python -m experiments.run trial --backend purechain --n 200 --rate 20
    python -m experiments.run campaign --only E1 E3 --backends purechain --scale quick
    python -m experiments.run tamper --backend purechain --n 500
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

import pandas as pd

from finverity import accounts
from finverity.backends import make_backend
from finverity.config import RESULTS
from finverity.loadgen import TrialConfig, commitment_checker, run_trial
from finverity.storage import EvidenceStore
from finverity.tamper import run_tamper

from .plan import plan

LOG = RESULTS / "campaign.log"


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _insider(cfg: TrialConfig, bk, done, m: int) -> None:
    from finverity.insider import run_insider
    store = EvidenceStore(cfg.run_id)
    items = pd.read_parquet(done.parent / "items.parquet")
    ids = [bytes.fromhex(h) for h in items.loc[items["status"] == "committed", "record_id"]][:m]
    recs = [{"record_id": r, "recorded_ts": float(bk.fetch_record(r)["timestamp"])} for r in ids]
    wait = max(r["recorded_ts"] for r in recs) + 35 - time.time()
    if wait > 0:
        time.sleep(wait)   # fabrication happens measurably after the original recording
    rs = accounts.roles(cfg.workers)
    rows = run_insider(bk, store, recs, rs.submitters[0], rs.approvers[0], time.time())
    df = pd.DataFrame(rows)
    df.to_parquet(done.parent / "insider.parquet", index=False)
    summ = df.groupby("scenario")["outcome"].value_counts().unstack(fill_value=0).to_dict("index")
    log(f"[{cfg.run_id}] insider: {summ}")
    store.delete()


def _trial(kw: dict, tamper: bool = False) -> dict | None:
    kw = dict(kw)
    insider_m = kw.pop("insider", 0)
    validators = kw.pop("validators", None)
    cfg = TrialConfig(**kw)
    done = RESULTS / "raw" / cfg.run_id / "summary.json"
    tamper = tamper or cfg.workload == "E6"   # E6 trials are complete only with tamper data
    if done.exists() and (not tamper or (done.parent / "tamper.parquet").exists()) and \
            (not insider_m or (done.parent / "insider.parquet").exists()):
        log(f"skip {cfg.run_id} (done)")
        return json.loads(done.read_text())
    from infra import isolate
    isolate(cfg.backend)
    if cfg.backend == "besu":
        from infra.besu import ensure_network
        ensure_network(validators or 4)
    elif cfg.backend == "poa2_local":
        from infra import poa2
        poa2.ensure(validators or 4)
    elif cfg.backend == "ethpos":
        from infra import ethpos
        ethpos.ensure()
    elif cfg.backend == "db_postgres":
        from infra import postgres
        postgres.up()
    bk = make_backend(cfg.backend, **({"validators": validators} if validators and cfg.backend in ("besu", "poa2_local") else {}))
    if tamper or insider_m:
        cfg.keep_store = True
    s = run_trial(cfg, bk, log=log)
    if tamper or cfg.workload == "E6":
        store = EvidenceStore(cfg.run_id)
        items = pd.read_parquet(done.parent / "items.parquet")
        ids = [bytes.fromhex(h) for h in items.loc[items["status"] == "committed", "record_id"]][: cfg.verify_n]
        approvers = [a.address for a in accounts.roles(cfg.workers).approvers]
        t0 = time.perf_counter()
        rows = run_tamper(commitment_checker(bk, store), store, ids, approvers, seed=cfg.seed + cfg.trial)
        df = pd.DataFrame(rows)
        df.to_parquet(done.parent / "tamper.parquet", index=False)
        mal = df[df["kind"] == "malicious"]
        ben = df[df["kind"] == "benign"]
        log(f"[{cfg.run_id}] tamper: {len(mal)} modifications, TDR={100 * mal['detected'].mean():.2f}% | "
            f"benign FPR={100 * ben['detected'].mean():.2f}% | control ok="
            f"{(~df[df['kind'] == 'control']['detected']).mean():.3f} | {time.perf_counter() - t0:.1f}s")
        store.delete()
    if insider_m:
        _insider(cfg, bk, done, insider_m)
    return s


def cmd_probe(args):
    from finverity.backends.evm import Rpc, recover_clique_signer
    from finverity.config import PURECHAIN
    rpc = Rpc(PURECHAIN.rpc_url)
    head = int(rpc.call("eth_blockNumber"), 16)
    blocks = rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in range(head - args.blocks + 1, head + 1)])
    signers = [recover_clique_signer(b) for b in blocks]
    ts = [int(b["timestamp"], 16) for b in blocks]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    rtt = []
    for _ in range(30):
        t = time.perf_counter(); rpc.call("eth_blockNumber"); rtt.append(time.perf_counter() - t)
    out = {"chain_id": int(rpc.call("eth_chainId"), 16), "client": rpc.call("web3_clientVersion"),
           "head": head, "gas_price": int(rpc.call("eth_gasPrice"), 16), "peers": int(rpc.call("net_peerCount"), 16),
           "block_gas_limit": int(blocks[-1]["gasLimit"], 16),
           "validators_observed": sorted(set(signers)), "n_validators": len(set(signers)),
           "in_turn_share": sum(int(b["difficulty"], 16) == 2 for b in blocks) / len(blocks),
           "block_gap_s_median": sorted(gaps)[len(gaps) // 2], "block_gap_s_max": max(gaps),
           "rpc_rtt_ms_median": 1000 * sorted(rtt)[len(rtt) // 2], "rpc_rtt_ms_min": 1000 * min(rtt)}
    print(json.dumps(out, indent=1))
    (RESULTS / "network_probe.json").parent.mkdir(parents=True, exist_ok=True)
    (RESULTS / "network_probe.json").write_text(json.dumps(out, indent=1))


def cmd_trial(args):
    kw = dict(backend=args.backend, source=args.source, n=args.n, level=args.level, doc_bytes=args.doc_bytes,
              rate=args.rate, workers=args.workers, trial=args.trial, seed=args.seed, workload="adhoc",
              verify_n=args.verify_n, tag=args.tag, batch_size=args.batch_size, batch_wait_s=args.batch_wait,
              insider=args.insider)
    _trial(kw, tamper=args.tamper)


def _keep_awake() -> None:
    """Ask Windows not to sleep while the campaign runs (reverts automatically on exit)."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def cmd_campaign(args):
    _keep_awake()
    p = plan(args.scale)
    for exp in args.only or list(p):
        cfgs = [c for c in p[exp] if not args.backends or c["backend"] in args.backends]
        cfgs.sort(key=lambda c: (c.get("validators", 0), c["workload"], c["backend"]))  # small workloads first; few restarts
        log(f"== {exp}: {len(cfgs)} trials (scale={args.scale})")
        zero = {}   # consecutive zero-commit trials per backend -> infrastructure fault guard
        for c in cfgs:
            if args.scale == "quick":
                c["tag"] = (c.get("tag", "") + "_quick").lstrip("_")
            if zero.get(c["backend"], 0) >= 2:
                log(f"SKIP {exp} {c['backend']}: 2 consecutive zero-commit trials (infrastructure fault)")
                continue
            try:
                s = _trial(c)
                zero[c["backend"]] = zero.get(c["backend"], 0) + 1 if s and s.get("committed", 0) == 0 else 0
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"ERROR {exp} {c}: {type(e).__name__}: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="experiments.run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe"); p.add_argument("--blocks", type=int, default=200); p.set_defaults(fn=cmd_probe)
    for name in ("trial", "tamper"):
        t = sub.add_parser(name)
        t.add_argument("--backend", default="purechain")
        t.add_argument("--source", default="momtsim")
        t.add_argument("--n", type=int, default=200)
        t.add_argument("--level", type=int, default=4)
        t.add_argument("--doc-bytes", type=int, default=100 * 1024)
        t.add_argument("--rate", type=float, default=20)
        t.add_argument("--workers", type=int, default=16)
        t.add_argument("--trial", type=int, default=0)
        t.add_argument("--seed", type=int, default=1)
        t.add_argument("--verify-n", type=int, default=1000)
        t.add_argument("--tag", default="")
        t.add_argument("--batch-size", type=int, default=1)
        t.add_argument("--batch-wait", type=float, default=1.0)
        t.add_argument("--insider", type=int, default=0, help="run E8 insider attacks on N records")
        t.set_defaults(fn=cmd_trial, tamper=(name == "tamper"))
    c = sub.add_parser("campaign")
    c.add_argument("--only", nargs="*")
    c.add_argument("--backends", nargs="*")
    c.add_argument("--scale", choices=["quick", "full"], default="full")
    c.set_defaults(fn=cmd_campaign)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
