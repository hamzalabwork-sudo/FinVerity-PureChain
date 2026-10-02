"""Statistical analysis (methodology section "Statistical and Experimental Analysis").

* Trial-level aggregation: mean, SD, median, 95% t-interval over independent trials.
* Latency distributions: median, P95, P99 pooled over transactions, with bootstrap CIs.
* Pairwise backend comparisons: Welch's t-test and Mann-Whitney U on trial-level values,
  Holm-Bonferroni correction, Cliff's delta effect size.
* Tamper detection: TDR / FPR with exact Clopper-Pearson 95% intervals.

    python -m analysis.stats
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from finverity.config import RESULTS

RAW = RESULTS / "raw"
TABLES = RESULTS / "tables"

METRICS = ["throughput_tps", "commit_latency_s_median", "commit_latency_s_p95", "commit_latency_s_p99",
           "success_rate", "energy_j_per_tx", "client_cpu_s", "infra_cpu_s", "infra_mem_peak_mb",
           "cost_total_usd_per_tx", "verify_total_s_median", "verify_success_rate", "binding_success_rate",
           "prep_evidence_s_mean", "prep_store_s_mean", "gas_used_mean", "onchain_payload_bytes_mean",
           "ack_latency_s_median", "first_seen_latency_s_median",
           "conf1_latency_s_median", "conf3_latency_s_median", "conf6_latency_s_median", "conf3_latency_s_p95",
           "reorged_blocks", "orphan_txs", "max_reorg_depth", "energy_dynamic_j_per_tx", "host_cpu_pct_mean"]


def load_summaries() -> pd.DataFrame:
    rows = [json.loads(p.read_text()) for p in sorted(RAW.glob("*/summary.json"))]
    df = pd.DataFrame(rows)
    if len(df):
        df = df[~df["tag"].fillna("").str.contains("smoke|quick") & (df["workload"] != "adhoc")]
    return df


def ci95(x: pd.Series) -> tuple[float, float]:
    x = x.dropna()
    if len(x) < 2:
        return (float("nan"), float("nan"))
    h = stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))
    return (x.mean() - h, x.mean() + h)


def aggregate(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    out = []
    for key, g in df.groupby(by, dropna=False):
        row = dict(zip(by, key if isinstance(key, tuple) else (key,)))
        row["trials"] = len(g)
        for m in METRICS:
            if m not in g:
                continue
            x = g[m].astype(float)
            lo, hi = ci95(x)
            row.update({f"{m}_mean": x.mean(), f"{m}_sd": x.std(ddof=1), f"{m}_median": x.median(),
                        f"{m}_ci_lo": lo, f"{m}_ci_hi": hi})
        out.append(row)
    return pd.DataFrame(out)


def cliffs_delta(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    if not len(a) or not len(b):
        return float("nan")
    gt = sum((x > b).sum() for x in a)
    lt = sum((x < b).sum() for x in a)
    return (gt - lt) / (len(a) * len(b))


def holm(p: list[float]) -> list[float]:
    order = np.argsort(p)
    adj = np.empty(len(p))
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (len(p) - rank) * p[i])
        adj[i] = min(1.0, running)
    return adj.tolist()


def compare_backends(df: pd.DataFrame, group: str, metrics: list[str]) -> pd.DataFrame:
    rows = []
    for g, sub in df.groupby(group):
        for m in metrics:
            for a, b in itertools.combinations(sorted(sub["backend"].unique()), 2):
                xa = sub.loc[sub["backend"] == a, m].dropna().astype(float)
                xb = sub.loc[sub["backend"] == b, m].dropna().astype(float)
                if len(xa) < 2 or len(xb) < 2:
                    continue
                t = stats.ttest_ind(xa, xb, equal_var=False)
                u = stats.mannwhitneyu(xa, xb, alternative="two-sided")
                rows.append({group: g, "metric": m, "a": a, "b": b, "mean_a": xa.mean(), "mean_b": xb.mean(),
                             "welch_t": t.statistic, "p_welch": t.pvalue, "p_mwu": u.pvalue,
                             "cliffs_delta": cliffs_delta(xa, xb), "n_a": len(xa), "n_b": len(xb)})
    out = pd.DataFrame(rows)
    if len(out):
        out["p_welch_holm"] = holm(out["p_welch"].tolist())
        out["p_mwu_holm"] = holm(out["p_mwu"].tolist())
    return out


def pooled_latency(run_ids: list[str], seed: int = 0, boot: int = 2000) -> dict:
    lat = np.concatenate([pd.read_parquet(RAW / r / "items.parquet").query("status == 'committed'")
                          ["commit_latency_s"].dropna().to_numpy() for r in run_ids]) if run_ids else np.array([])
    if not len(lat):
        return {}
    rng = np.random.default_rng(seed)
    sample = lat if len(lat) <= 50_000 else rng.choice(lat, 50_000, replace=False)
    bs = rng.choice(sample, (boot, len(sample)), replace=True) if len(sample) <= 5_000 else None
    res = {"n_tx": len(lat), "median": float(np.median(lat)), "p95": float(np.percentile(lat, 95)),
           "p99": float(np.percentile(lat, 99)), "mean": float(lat.mean())}
    if bs is not None:
        for q, name in ((50, "median"), (95, "p95"), (99, "p99")):
            v = np.percentile(bs, q, axis=1)
            res[f"{name}_ci"] = (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5)))
    return res


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    lo = 0.0 if k == 0 else stats.beta.ppf(alpha / 2, k, n - k + 1)
    hi = 1.0 if k == n else stats.beta.ppf(1 - alpha / 2, k + 1, n - k)
    return (float(lo), float(hi))


def tamper_table() -> pd.DataFrame:
    frames = [pd.read_parquet(p).assign(run_id=p.parent.name) for p in RAW.glob("*/tamper.parquet")]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    rows = []
    for (kind, mod), g in df.groupby(["kind", "modification"]):
        k, n = int(g["detected"].sum()), len(g)
        lo, hi = clopper_pearson(k, n)
        rows.append({"kind": kind, "modification": mod, "cases": n, "detected": k, "rate_pct": 100 * k / n,
                     "ci95_lo_pct": 100 * lo, "ci95_hi_pct": 100 * hi,
                     "verify_us_median": 1e6 * g["verify_s"].median()})
    mal = df[df["kind"] == "malicious"]
    k, n = int(mal["detected"].sum()), len(mal)
    lo, hi = clopper_pearson(k, n)
    rows.append({"kind": "malicious", "modification": "ALL (TDR)", "cases": n, "detected": k,
                 "rate_pct": 100 * k / n, "ci95_lo_pct": 100 * lo, "ci95_hi_pct": 100 * hi,
                 "verify_us_median": 1e6 * mal["verify_s"].median()})
    ben = df[df["kind"] == "benign"]
    k, n = int(ben["detected"].sum()), len(ben)
    lo, hi = clopper_pearson(k, n)
    rows.append({"kind": "benign", "modification": "ALL (FPR)", "cases": n, "detected": k,
                 "rate_pct": 100 * k / n, "ci95_lo_pct": 100 * lo, "ci95_hi_pct": 100 * hi,
                 "verify_us_median": 1e6 * ben["verify_s"].median()})
    return pd.DataFrame(rows)


def _latex(df: pd.DataFrame, path: Path, cols: list[str], fmt: dict[str, str] | None = None) -> None:
    fmt = fmt or {}
    d = df[[c for c in cols if c in df]].copy()
    for c, f in fmt.items():
        if c in d:
            d[c] = d[c].map(lambda v: f.format(v) if pd.notna(v) else "--")
    path.write_text(d.to_latex(index=False, escape=True))


def main() -> None:
    TABLES.mkdir(parents=True, exist_ok=True)
    df = load_summaries()
    if not len(df):
        print("no results yet")
        return
    df.to_csv(RESULTS / "summary_all.csv", index=False)
    df["factor"] = np.select(
        [df["workload"] == "E2", df["workload"] == "E3", df["workload"] == "E4", df["workload"] == "E5"],
        [df["rate"].astype(str), df["tag"].str.replace("_quick", ""), df["level"].astype(str), df["tag"].str.replace("_quick", "")],
        default=df["source"])
    agg = aggregate(df, ["workload", "factor", "backend"])
    agg.to_csv(TABLES / "aggregate.csv", index=False)

    e1 = df[df["workload"].str.match(r"W\d")]
    if len(e1):
        cmp = compare_backends(e1, "workload", ["throughput_tps", "commit_latency_s_median", "commit_latency_s_p95",
                                                "energy_j_per_tx", "cost_total_usd_per_tx"])
        cmp.to_csv(TABLES / "e1_backend_comparisons.csv", index=False)
        a1 = agg[agg["workload"].str.match(r"W\d")]
        _latex(a1, TABLES / "e1_performance.tex",
               ["workload", "backend", "trials", "throughput_tps_mean", "commit_latency_s_median_mean",
                "commit_latency_s_p95_mean", "commit_latency_s_p99_mean", "success_rate_mean", "energy_j_per_tx_mean"],
               {"throughput_tps_mean": "{:.1f}", "commit_latency_s_median_mean": "{:.3f}",
                "commit_latency_s_p95_mean": "{:.3f}", "commit_latency_s_p99_mean": "{:.3f}",
                "success_rate_mean": "{:.4f}", "energy_j_per_tx_mean": "{:.3f}"})
        pooled = []
        for (w, b), g in e1.groupby(["workload", "backend"]):
            pooled.append({"workload": w, "backend": b, **pooled_latency(g["run_id"].tolist())})
        pd.DataFrame(pooled).to_csv(TABLES / "e1_pooled_latency.csv", index=False)

    chain = df[df["kind"] == "blockchain"]
    if len(chain):
        forks = chain.groupby("backend").agg(
            trials=("run_id", "count"), blocks=("blocks_checked", "sum"), reorged_blocks=("reorged_blocks", "sum"),
            orphan_blocks=("orphan_blocks", "sum"), orphan_txs=("orphan_txs", "sum"),
            max_reorg_depth=("max_reorg_depth", "max"), out_of_turn_orphans=("orphans_out_of_turn", "sum"),
            committed=("committed", "sum"), txs_first_seen_in_orphan=("txs_orphaned_first", "sum"))
        forks["reorg_rate_per_1000_blocks"] = 1000 * forks["reorged_blocks"] / forks["blocks"]
        forks.to_csv(TABLES / "finality_forks.csv")
        _latex(forks.reset_index(), TABLES / "finality_forks.tex",
               ["backend", "trials", "blocks", "reorged_blocks", "max_reorg_depth", "orphan_txs",
                "reorg_rate_per_1000_blocks"], {"reorg_rate_per_1000_blocks": "{:.2f}"})

    ins = [pd.read_parquet(p).assign(run_id=p.parent.name) for p in RAW.glob("*/insider.parquet")]
    if ins:
        di = pd.concat(ins)
        di["backend"] = di["run_id"].map(df.set_index("run_id")["backend"])
        tab = di.groupby(["scenario", "backend"])["outcome"].value_counts().unstack(fill_value=0)
        tab["cases"] = tab.sum(axis=1)
        for c in ("prevented", "detected", "undetected"):
            if c not in tab:
                tab[c] = 0
        tab["protected_pct"] = 100 * (tab["prevented"] + tab["detected"]) / tab["cases"]
        tab.to_csv(TABLES / "e8_insider.csv")
        _latex(tab.reset_index(), TABLES / "e8_insider.tex",
               ["scenario", "backend", "cases", "prevented", "detected", "undetected", "protected_pct"],
               {"protected_pct": "{:.1f}"})

    tt = tamper_table()
    if len(tt):
        tt.to_csv(TABLES / "e6_tamper_detection.csv", index=False)
        _latex(tt, TABLES / "e6_tamper_detection.tex",
               ["kind", "modification", "cases", "detected", "rate_pct", "ci95_lo_pct", "ci95_hi_pct"],
               {"rate_pct": "{:.2f}", "ci95_lo_pct": "{:.2f}", "ci95_hi_pct": "{:.2f}"})
    print(f"{len(df)} trials aggregated -> {TABLES}")


if __name__ == "__main__":
    main()
