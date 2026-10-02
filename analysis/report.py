"""Build the Overleaf-ready experimental report (paper/) from the measured data and compile it to PDF.

    python -m analysis.report            # regenerate tables/figures/macros, compile paper/main.pdf, zip for Overleaf

Every number in the generated tables and in the macros file comes from results/raw; the report can be
rebuilt at any time and fills in as the campaign progresses.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from experiments.plan import plan
from finverity.config import RESULTS, ROOT
from . import figures, stats
from .stats import RAW, clopper_pearson, load_summaries

PAPER = ROOT / "paper"
GEN = PAPER / "generated"
FIGDIR = PAPER / "figures"
LIVE_CALIB = ROOT / "results" / "_archive_v0" / "raw" / "adhoc_purechain_momtsim_n300_L4_d10240_r50_s1_t0_smoke4"
EMU_CALIB = RAW / "adhoc_poa2_local_momtsim_n300_L4_d10240_r50_s1_t0_calib2"

NAME = {"purechain": "PureChain (live)", "poa2_local": "PoA\\textsuperscript{2} emulation", "besu": "Besu QBFT",
        "db_postgres": "PostgreSQL", "ethpos": "Ethereum PoS"}
ORDER = ["purechain", "poa2_local", "besu", "db_postgres", "ethpos"]
EXP_TITLE = {"E1": "Controlled workloads W1--W5", "E2": "Offered-load sweep", "E3": "Evidence-size sweep",
             "E4": "Evidence-complexity sweep", "E5": "Validator-count sweep", "E6": "Tamper detection",
             "E7": "Cross-dataset replication", "E8": "Insider tampering", "E9": "Batched Merkle commitments"}


def tex(s) -> str:
    return (str(s).replace("\\", "\\textbackslash{}").replace("_", "\\_").replace("%", "\\%")
            .replace("&", "\\&").replace("#", "\\#"))


def f(v, nd=2, pct=False) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "--"
    if pct:
        return f"{100 * v:.{nd}f}"
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.{nd}f}"


def table(path: Path, caption: str, label: str, header: list[str], rows: list[list[str]], align: str,
          note: str = "", size: str = "\\small") -> None:
    body = "\n".join(" & ".join(r) + " \\\\" for r in rows) if rows else \
        f"\\multicolumn{{{len(header)}}}{{c}}{{\\emph{{pending -- experiment still running}}}} \\\\"
    s = (f"\\begin{{table}}[t]\n\\centering\n{size}\n\\caption{{{caption}}}\n\\label{{{label}}}\n" +
         ("\\resizebox{\\linewidth}{!}{%\n" if len(header) >= 8 else "") +
         f"\\begin{{tabular}}{{{align}}}\n\\toprule\n" + " & ".join(header) + " \\\\\n\\midrule\n" + body +
         "\n\\bottomrule\n\\end{tabular}" + ("}" if len(header) >= 8 else "") + "\n" + (f"\\par\\smallskip\\footnotesize {note}\n" if note else "") +
         "\\end{table}\n")
    path.write_text(s, encoding="utf-8")


def mean_ci(x: pd.Series) -> str:
    x = x.dropna().astype(float)
    if not len(x):
        return "--"
    if len(x) < 2:
        return f(x.mean())
    lo, hi = stats.ci95(x)
    return f"{f(x.mean())} [{f(max(lo, 0.0))}, {f(hi)}]"   # all reported metrics are non-negative


# ------------------------------------------------------------------ individual tables
def t_status(df: pd.DataFrame) -> None:
    P = plan("full")
    rows = []
    for e, cfgs in P.items():
        by = {}
        for c in cfgs:
            by.setdefault(c["backend"], 0)
            by[c["backend"]] += 1
        done = df[df["workload"].isin([e] if not e == "E1" else ["W1", "W2", "W3", "W4", "W5"])]
        cells = []
        for b in ORDER:
            if b in by:
                d = int((done["backend"] == b).sum())
                cells.append(f"{min(d, by[b])}/{by[b]}")
            else:
                cells.append("")
        rows.append([e, EXP_TITLE[e], *cells])
    table(GEN / "t_status.tex", "Campaign completion status (completed / planned trials) at the time this report "
          "was generated. PureChain (live) trials are pending the recovery of the public RPC endpoint.",
          "tab:status", ["ID", "Experiment", *[NAME[b] for b in ORDER]], rows, "llccccc", size="\\scriptsize")


def t_calibration() -> None:
    if not (LIVE_CALIB / "summary.json").exists() or not (EMU_CALIB / "summary.json").exists():
        return
    live = json.loads((LIVE_CALIB / "summary.json").read_text())
    emu = json.loads((EMU_CALIB / "summary.json").read_text())
    spec = [("Records committed", "committed", 0, False), ("Throughput (records/s)", "throughput_tps", 1, False),
            ("Inclusion latency, median (s)", "commit_latency_s_median", 2, False),
            ("Inclusion latency, P95 (s)", "commit_latency_s_p95", 2, False),
            ("1-confirmation latency, median (s)", "conf1_latency_s_median", 2, False),
            ("3-confirmation latency, median (s)", "conf3_latency_s_median", 2, False),
            ("6-confirmation latency, median (s)", "conf6_latency_s_median", 2, False),
            ("Transactions per non-empty block, mean", "txs_per_block_mean", 1, False),
            ("Distinct block signers", "block_signers", 0, False),
            ("Gas per record", "gas_used_mean", 0, False), ("Verification success (\\%)", "verify_success_rate", 1, True)]
    rows = []
    for name, k, nd, pct in spec:
        a, b = live.get(k), emu.get(k)
        dev = f"{100 * (b - a) / a:+.1f}\\%" if isinstance(a, (int, float)) and a and not pct else ""
        rows.append([name, f(a, nd, pct), f(b, nd, pct), dev])
    table(GEN / "t_calibration.tex",
          "Calibration of the PoA\\textsuperscript{2} emulation against live PureChain (identical workload: "
          "300 MoMTSim records, 10\\,KB evidence, 50 records/s Poisson arrivals).", "tab:calibration",
          ["Metric", "Live PureChain", "PoA\\textsuperscript{2} emulation", "Deviation"], rows, "lrrr",
          note="Live inclusion latency additionally contains the wide-area round trip and the public load balancer.")


def t_live(df_all: pd.DataFrame) -> None:
    live = df_all[(df_all["backend"] == "purechain") & (df_all["committed"] > 0)]
    if not len(live):
        return
    rows = []
    for _, s in live.sort_values("run_id").iterrows():
        rows.append([tex(s["source"]), f(int(s["committed"]), 0), f(s["throughput_tps"], 1),
                     f(s["commit_latency_s_median"]), f(s["conf3_latency_s_median"]), f(s["txs_per_block_mean"], 1),
                     f(int(s.get("reorged_blocks") or 0), 0), f(int(s.get("max_reorg_depth") or 0), 0),
                     f(s["verify_success_rate"], 1, True)])
    table(GEN / "t_live.tex", "Measurements on the live PureChain network (4 validators) before the public RPC "
          "became unavailable (E6 runs, 1{,}000 records each, 100\\,KB evidence, 50 records/s offered).", "tab:live",
          ["Dataset", "Committed", "rec/s", "Incl. P50 (s)", "3-conf P50 (s)", "tx/block", "Reorged", "Depth",
           "Verified (\\%)"], rows, "lrrrrrrrr",
          note="Submitted through the load-balanced endpoint; nonce splitting across two RPC nodes inflates latency "
               "(see Section~\\ref{sec:validity}).", size="\\scriptsize")


def t_tamper() -> dict:
    tt = stats.tamper_table()
    if not len(tt):
        return {}
    runs = {}
    for p in RAW.glob("*/tamper.parquet"):
        b = json.loads((p.parent / "summary.json").read_text())["backend"]
        runs[b] = runs.get(b, 0) + 1
    pooled = "; ".join(f"{NAME[k]}: {v} runs" for k, v in sorted(runs.items(), key=lambda kv: ORDER.index(kv[0])))
    rows = []
    for _, r in tt.iterrows():
        rows.append([tex(r["kind"]), tex(r["modification"]), f(int(r["cases"]), 0), f(int(r["detected"]), 0),
                     f(r["rate_pct"]), f"[{f(r['ci95_lo_pct'], 3)}, {f(r['ci95_hi_pct'], 3)}]"])
    table(GEN / "t_tamper.tex", "Tamper detection against on-chain commitments (E6). Malicious rows report the "
          "detection rate (TDR); benign rows report the false-positive rate (FPR). Exact Clopper--Pearson 95\\% intervals. "
          f"Pooled over {pooled}.",
          "tab:tamper", ["Kind", "Modification", "Cases", "Flagged", "Rate (\\%)", "95\\% CI"],
          rows, "llrrrc", size="\\scriptsize")
    a = tt[tt["modification"] == "ALL (TDR)"].iloc[0]
    b = tt[tt["modification"] == "ALL (FPR)"].iloc[0]
    return {"TamperCases": f(int(a["cases"]), 0), "TDR": f(a["rate_pct"]), "TDRlo": f(a["ci95_lo_pct"], 3),
            "FPR": f(b["rate_pct"]), "FPRhi": f(b["ci95_hi_pct"], 3), "BenignCases": f(int(b["cases"]), 0)}


def t_insider(df: pd.DataFrame) -> dict:
    fr = [pd.read_parquet(p).assign(run_id=p.parent.name) for p in RAW.glob("*/insider.parquet")]
    if not fr:
        return {}
    d = pd.concat(fr)
    d["backend"] = d["run_id"].map(df.set_index("run_id")["backend"])
    d = d.dropna(subset=["backend"])
    scen = {"S1_evidence_only": "S1 alter evidence", "S2_commitment_rewrite": "S2 alter evidence + rewrite commitment",
            "S3_backdated_record": "S3 fabricate + backdate record"}
    rows, macros = [], {}
    for sc, label in scen.items():
        for b in ORDER:
            g = d[(d["scenario"] == sc) & (d["backend"] == b)]
            if not len(g):
                continue
            n = len(g)
            pv, de, un = [(g["outcome"] == o).sum() for o in ("prevented", "detected", "undetected")]
            lo, hi = clopper_pearson(int(pv + de), n)
            rows.append([label, NAME[b], f(n, 0), f(int(pv), 0), f(int(de), 0), f(int(un), 0),
                         f"{100 * (pv + de) / n:.1f} [{100 * lo:.1f}, {100 * hi:.1f}]"])
            key = {"S1": "SOne", "S2": "STwo", "S3": "SThree"}[sc.split("_")[0]] + {"poa2_local": "Emu", "besu": "Besu", "db_postgres": "DB", "ethpos": "Pos",
                                      "purechain": "Live"}[b]
            macros[f"Ins{key}"] = f"{100 * (pv + de) / n:.1f}"
    table(GEN / "t_insider.tex", "Privileged-insider tampering (E8): share of attacks prevented (write rejected) or "
          "detected by the audit, with Clopper--Pearson 95\\% intervals.", "tab:insider",
          ["Attack", "Infrastructure", "Cases", "Prevented", "Detected", "Undetected", "Protected (\\%)"], rows,
          "llrrrrc", size="\\scriptsize")
    return macros


def t_batching(df: pd.DataFrame) -> None:
    sub = df[df["workload"] == "E9"]
    rows = []
    for (b, rate, k), g in sub.groupby(["backend", "rate", "batch_size"]):
        rows.append([NAME[b], f(rate, 0), f(int(k), 0), str(len(g)), mean_ci(g["throughput_tps"]),
                     mean_ci(g["e2e_latency_s_median"]), mean_ci(g["final_e2e_latency_s_median"]),
                     f(g["gas_used_mean"].mean(), 0) if b != "db_postgres" else "--"])
    rows.sort(key=lambda r: (ORDER.index(next(k for k, v in NAME.items() if v == r[0])), float(r[1].replace(",", "")),
                             int(r[2].replace(",", ""))))
    table(GEN / "t_batching.tex", "Batched Merkle commitments (E9): records per commitment $k$ versus throughput, "
          "end-to-end inclusion latency and finality latency (mean over trials, 95\\% CI).", "tab:batching",
          ["Infrastructure", "Offered", "$k$", "$n$", "Throughput (rec/s)", "Arrival$\\rightarrow$incl. P50 (s)",
           "Arrival$\\rightarrow$final P50 (s)", "Gas/record"], rows, "lrrrcccr", size="\\scriptsize")


def _fmt_factor(exp: str, fv) -> str:
    if exp == "E2":
        return "max" if float(fv) == 0 else f"{float(fv):g}"
    if exp == "E3":
        kb = int(fv) / 1024
        return "none" if kb == 0 else (f"{kb:g}\\,KB" if kb < 1024 else f"{kb / 1024:g}\\,MB")
    if exp == "E5":
        tag, rate = fv
        return f"{tex(str(tag).replace('_confirm', ' (confirm)'))} @ {float(rate):g}"
    return tex(fv)


def t_generic(df: pd.DataFrame, exp: str, factor, factor_name: str, caption: str, label: str) -> None:
    sub = df[df["workload"].isin(["W1", "W2", "W3", "W4", "W5"] if exp == "E1" else [exp])]
    rows = []
    keys = factor if isinstance(factor, list) else [factor]
    if len(sub):
        sub = sub.copy()
        if exp == "E2":
            sub["_ord"] = sub["rate"].replace(0, 1e9)
            keys = ["_ord"]
        for key, g in sub.groupby(keys + ["backend"]):
            fv, b = (tuple(key[:-1]) if len(keys) > 1 else key[0]), key[-1]
            if exp == "E2":
                fv = 0 if fv == 1e9 else fv
            rows.append([_fmt_factor(exp, fv), NAME[b], str(len(g)), f(g["success_rate"].mean(), 1, True), mean_ci(g["throughput_tps"]),
                         mean_ci(g["e2e_latency_s_median"]), mean_ci(g["e2e_latency_s_p95"]),
                         mean_ci(g["final_e2e_latency_s_median"]), f(g["energy_j_per_tx"].mean(), 3),
                         f(g["cost_total_usd_per_tx"].mean() * 1e6, 2)])
    table(GEN / f"t_{exp.lower()}.tex", caption, label,
          [factor_name, "Infrastructure", "$n$", "Success (\\%)", "Throughput (rec/s)", "Incl. P50 (s)",
           "Incl. P95 (s)", "Final P50 (s)", "J/record", "\\textmu USD/record"], rows, "llrrcccccr",
          size="\\scriptsize")


def live_forks() -> dict:
    """Fork statistics over every live-PureChain trial ever run (campaign and ad-hoc, incl. archived)."""
    rows = []
    for sp in [*RAW.glob("*/summary.json"), *(RESULTS / "_archive_v0" / "raw").glob("*/summary.json")]:
        s = json.loads(sp.read_text())
        if s.get("backend") == "purechain" and s.get("blocks_checked") is not None and s.get("max_reorg_depth") is not None:
            rows.append((s["blocks_checked"], s["reorged_blocks"], s["max_reorg_depth"], s.get("committed", 0)))
    if not rows:
        return {}
    blocks = sum(r[0] for r in rows)
    reorg = sum(r[1] for r in rows)
    depth = max(r[2] for r in rows)
    return {"LiveRuns": f(len(rows), 0), "LiveBlocksObserved": f(blocks, 0), "LiveReorgBlocks": f(reorg, 0),
            "LiveReorgPct": f(100 * reorg / blocks, 1), "LiveMaxReorg": str(depth), "FinalityDepth": str(depth + 1)}



DSNAME = {"momtsim": "MoMTSim", "saml_d": "SAML-D", "ibm_aml": "IBM AML"}


def _workload_view(df: pd.DataFrame) -> pd.DataFrame:
    """E1 (MoMTSim) and E7 (SAML-D, IBM AML) trials under one (workload, dataset) key."""
    e1 = df[df["workload"].isin(["W1", "W2", "W5"])].assign(wl=lambda d: d["workload"])
    e7 = df[df["workload"] == "E7"].assign(wl=lambda d: d["tag"].fillna(""))
    return pd.concat([e1, e7])


def t_datasets(df: pd.DataFrame) -> None:
    from scipy import stats as st
    v = _workload_view(df)
    rows = []
    for (wl, b), g in v.groupby(["wl", "backend"]):
        per = {s: gg for s, gg in g.groupby("source")}
        groups = [gg["e2e_latency_s_median"].dropna().astype(float) for gg in per.values()]
        p = st.kruskal(*groups).pvalue if len(groups) >= 2 and all(len(x) >= 2 for x in groups) and \
            len({round(x, 12) for grp in groups for x in grp}) > 1 else float("nan")
        for i, (s, gg) in enumerate(sorted(per.items(), key=lambda kv: list(DSNAME).index(kv[0]))):
            rows.append([tex(wl) if i == 0 else "", NAME[b] if i == 0 else "", DSNAME[s], str(len(gg)),
                         mean_ci(gg["throughput_tps"]), mean_ci(gg["e2e_latency_s_median"]),
                         mean_ci(gg["final_e2e_latency_s_median"]), f(gg["verify_success_rate"].mean(), 1, True),
                         (f"{p:.3f}" if p == p else "--") if i == 0 else ""])
    table(GEN / "t_datasets.tex", "Cross-dataset replication (E1 on MoMTSim, E7 on SAML-D and IBM AML): mean over "
          "trials with 95\\% CI. $p$: Kruskal--Wallis test of a dataset effect on median inclusion latency.",
          "tab:datasets", ["Workload", "Infrastructure", "Dataset", "$n$", "Throughput (rec/s)", "Incl. P50 (s)",
                           "Final P50 (s)", "Verified (\\%)", "$p$"], rows, "lllrcccrr", size="\\scriptsize")


def t_tamper_by_dataset() -> None:
    fr = []
    for p in RAW.glob("*/tamper.parquet"):
        sm = json.loads((p.parent / "summary.json").read_text())
        fr.append(pd.read_parquet(p).assign(source=sm["source"], backend=sm["backend"]))
    if not fr:
        return
    d = pd.concat(fr)
    rows = []
    for (s, b), g in d.groupby(["source", "backend"]):
        mal, ben = g[g["kind"] == "malicious"], g[g["kind"] == "benign"]
        lo, _ = clopper_pearson(int(mal["detected"].sum()), len(mal))
        _, hi = clopper_pearson(int(ben["detected"].sum()), len(ben))
        rows.append([DSNAME[s], NAME[b], f(len(mal), 0), f(100 * mal["detected"].mean()), f(100 * lo, 3),
                     f(len(ben), 0), f(100 * ben["detected"].mean()), f(100 * hi, 3)])
    table(GEN / "t_tamper_ds.tex", "Tamper detection by dataset and infrastructure (E6).", "tab:tamperds",
          ["Dataset", "Infrastructure", "Malicious", "TDR (\\%)", "TDR 95\\% lower", "Benign", "FPR (\\%)",
           "FPR 95\\% upper"], rows, "llrrrrrr", size="\\scriptsize")


def t_efficiency(df: pd.DataFrame) -> None:
    v = _workload_view(df)
    v = v[v["source"] == "momtsim"] if len(v[v["source"] == "momtsim"]) else v
    rows = []
    for (wl, b), g in v.groupby(["wl", "backend"]):
        measured = bool(g["infra_energy_measured"].fillna(False).astype(bool).any())
        win = g["window_s"].astype(float)
        cpu_client = (g["window_client_cpu_s"].astype(float) / win).mean()
        cpu_infra = (g["window_infra_cpu_s"].astype(float) / win).mean() if measured else float("nan")
        per_rec = lambda col: (g[col].astype(float) / g["committed"].astype(float)).mean()
        rows.append([tex(wl), NAME[b], f(cpu_client, 2), f(cpu_infra, 2) if measured else "n/m",
                     f(g["infra_mem_peak_mb"].astype(float).mean(), 0) if measured else "n/m",
                     f(per_rec("network_bytes") / 1024, 2), f(g["onchain_payload_bytes_mean"].astype(float).mean(), 0),
                     f(g["evidence_bytes_mean"].astype(float).mean() / 1024, 1),
                     f(g["energy_j_per_tx"].astype(float).mean(), 3), f(g["energy_dynamic_j_per_tx"].astype(float).mean(), 4),
                     f(g["cost_total_usd_per_tx"].astype(float).mean() * 1e6, 2)])
    table(GEN / "t_efficiency.tex", "Efficiency metrics (MoMTSim workloads): CPU in cores used on average during the "
          "processing window, peak infrastructure memory, network I/O, storage overhead per record, energy and "
          "operational cost. n/m: not measurable (remote validators).", "tab:efficiency",
          ["Workload", "Infrastructure", "CPU client", "CPU infra", "Mem (MB)", "Net (KB/rec)", "On-chain (B/rec)",
           "Off-chain (KB/rec)", "E attr. (J)", "E dyn. (J)", "\\textmu USD/rec"], rows, "llrrrrrrrrr",
          size="\\scriptsize")


def key_macros(df: pd.DataFrame) -> dict:
    """Headline comparison numbers used in the results text (all derived from trial means)."""
    m = {}
    def mean(sel, col):
        x = df.loc[sel, col].dropna().astype(float)
        return float(x.mean()) if len(x) else float("nan")
    wl = df["workload"]
    B = lambda b: df["backend"] == b
    # E2: sustained per-record capacity (unthrottled) and latency under 200 rec/s
    for b, k in (("poa2_local", "Emu"), ("besu", "Besu"), ("db_postgres", "DB")):
        m[f"MaxTps{k}"] = f(mean((wl == "E2") & B(b) & (df["rate"] == 0), "throughput_tps"), 0)
        m[f"TpsTwoH{k}"] = f(mean((wl == "E2") & B(b) & (df["rate"] == 200), "throughput_tps"), 0)
        m[f"LatTwoH{k}"] = f(mean((wl == "E2") & B(b) & (df["rate"] == 200), "e2e_latency_s_median"), 2)
    r = mean((wl == "E2") & B("poa2_local") & (df["rate"] == 0), "throughput_tps") / \
        mean((wl == "E2") & B("besu") & (df["rate"] == 0), "throughput_tps")
    m["MaxTpsRatio"] = f(r, 1)
    # E1 W2 latency / finality
    for b, k in (("poa2_local", "Emu"), ("besu", "Besu"), ("db_postgres", "DB"), ("ethpos", "Pos")):
        m[f"InclWtwo{k}"] = f(mean((wl == "W2") & B(b), "e2e_latency_s_median"), 2)
        m[f"FinalWtwo{k}"] = f(mean((wl == "W2") & B(b), "final_e2e_latency_s_median"), 2 if b != "ethpos" else 0)
        m[f"TpsWtwo{k}"] = f(mean((wl == "W2") & B(b), "throughput_tps"), 1)
    fe, fp = mean((wl == "W2") & B("poa2_local"), "final_e2e_latency_s_median"), \
        mean((wl == "W2") & B("ethpos"), "final_e2e_latency_s_median")
    m["FinalRatioPos"] = f(fp / fe, 0)
    m["InclRatioPos"] = f(mean((wl == "W2") & B("ethpos"), "e2e_latency_s_median") /
                          mean((wl == "W2") & B("poa2_local"), "e2e_latency_s_median"), 0)
    # fees and cost
    m["FeePos"] = f(mean(wl.isin(["W1", "W2"]) & B("ethpos"), "cost_fee_usd_per_tx"), 2)
    for b, k in (("poa2_local", "Emu"), ("besu", "Besu"), ("db_postgres", "DB"), ("ethpos", "Pos")):
        m[f"Cost{k}"] = f(mean((wl == "W2") & B(b), "cost_total_usd_per_tx") * 1e6, 1)
        m[f"Edyn{k}"] = f(mean((wl == "W2") & B(b), "energy_dynamic_j_per_tx"), 3)
        m[f"Eatt{k}"] = f(mean((wl == "W2") & B(b), "energy_j_per_tx"), 2)
    m["EdynRatioBesu"] = f(mean((wl == "W2") & B("besu"), "energy_dynamic_j_per_tx") /
                           mean((wl == "W2") & B("poa2_local"), "energy_dynamic_j_per_tx"), 1)
    m["EattRatioPos"] = f(mean((wl == "W2") & B("ethpos"), "energy_j_per_tx") /
                          mean((wl == "W2") & B("poa2_local"), "energy_j_per_tx"), 1)
    # verification time
    for b, k in (("poa2_local", "Emu"), ("besu", "Besu"), ("db_postgres", "DB")):
        m[f"Verify{k}"] = f(1e3 * mean(wl.str.match(r"W\d") & B(b), "verify_total_s_median"), 1)
    # E5 validator scaling
    e5 = df[wl == "E5"]
    for v in (4, 7, 10):
        for b, k in (("poa2_local", "Emu"), ("besu", "Besu")):
            g = e5[(e5["backend"] == b) & (e5["tag"] == f"v{v}") & (e5["rate"] == 200)]
            m[f"VTps{k}{['','','','','Four','','','Seven','','','Ten'][v]}"] = f(g["throughput_tps"].mean(), 0) if len(g) else "--"
    g = e5[(e5["backend"] == "besu") & (e5["tag"] == "v10")]
    m["BesuTenSuccess"] = f(g["success_rate"].mean(), 0, True) if len(g) else "--"
    # E9 batching
    e9 = df[(wl == "E9") & (df["backend"] == "poa2_local")]
    m["GasKone"] = f(e9.loc[e9["batch_size"] == 1, "gas_used_mean"].mean(), 0)
    m["GasKhundred"] = f(e9.loc[e9["batch_size"] == 100, "gas_used_mean"].mean(), 0)
    ep = df[(wl == "E9") & (df["backend"] == "ethpos")]
    m["PosTpsKone"] = f(ep.loc[ep["batch_size"] == 1, "throughput_tps"].mean(), 1)
    m["PosTpsKfifty"] = f(ep.loc[ep["batch_size"] == 50, "throughput_tps"].mean(), 1)
    m["PosFeeKfifty"] = f(ep.loc[ep["batch_size"] == 50, "cost_fee_usd_per_tx"].mean(), 3)
    # E7 cross-dataset: smallest Kruskal-Wallis p for the PoA2 emulation
    from scipy import stats as st
    v = _workload_view(df)
    ps = []
    for wlk, g in v[v["backend"] == "poa2_local"].groupby("wl"):
        groups = [gg["e2e_latency_s_median"].dropna() for _, gg in g.groupby("source")]
        if len(groups) >= 2 and all(len(x) >= 2 for x in groups):
            ps.append(st.kruskal(*groups).pvalue)
    m["KWminEmu"] = f(min(ps), 2) if ps else "--"
    return m


def macros_file(m: dict, df: pd.DataFrame) -> None:
    probe = json.loads((RESULTS / "network_probe.json").read_text()) if (RESULTS / "network_probe.json").exists() else {}
    m.update({"Trials": f(len(df), 0), "LiveValidators": str(probe.get("n_validators", 4)),
              "LiveRTT": f(probe.get("rpc_rtt_ms_median", float("nan")), 1),
              "LiveGasLimit": f(probe.get("block_gas_limit", 30_000_000), 0),
              "LiveInTurn": f(probe.get("in_turn_share", float("nan")), 0, True)})
    lines = [f"\\newcommand{{\\{k}}}{{{v}}}" for k, v in m.items()]
    for k in ("TDR", "TDRlo", "FPR", "FPRhi", "TamperCases", "BenignCases"):
        if k not in m:
            lines.append(f"\\newcommand{{\\{k}}}{{--}}")
    (GEN / "macros.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ build
def build() -> Path:
    GEN.mkdir(parents=True, exist_ok=True)
    FIGDIR.mkdir(parents=True, exist_ok=True)
    stats.main()
    figures.main()
    for p in (RESULTS / "figures").glob("*.pdf"):
        shutil.copy(p, FIGDIR / p.name)
    df = load_summaries()
    all_rows = [json.loads(p.read_text()) for p in RAW.glob("*/summary.json")]
    df_all = pd.DataFrame(all_rows)
    m = live_forks()
    t_status(df)
    t_calibration()
    t_live(df_all[df_all["workload"] == "E6"])
    m.update(t_tamper())
    m.update(t_insider(df))
    t_batching(df)
    t_datasets(df)
    t_tamper_by_dataset()
    t_efficiency(df)
    t_generic(df, "E1", "workload", "Workload", "Controlled workloads (E1), offered load 50 records/s.", "tab:e1")
    t_generic(df, "E2", "rate", "Offered (rec/s)", "Offered-load sweep (E2).", "tab:e2")
    t_generic(df, "E3", "doc_bytes", "Evidence (bytes)", "Evidence-size sweep (E3).", "tab:e3")
    t_generic(df, "E4", "level", "Level", "Evidence-complexity sweep (E4).", "tab:e4")
    t_generic(df, "E5", ["tag", "rate"], "Validators @ offered", "Validator-count sweep (E5); every validator is "
              "limited to 1 CPU. Besu QBFT with 10 validators lost liveness in 3 of 4 trials (BFT event-queue overflow).",
              "tab:e5")
    m.update(key_macros(df))
    macros_file(m, df)
    figs = sorted(p.stem for p in FIGDIR.glob("*.pdf"))
    (GEN / "figures.tex").write_text("".join(
        f"\\IfFileExists{{figures/{s}.pdf}}{{\\def\\has{s.split('_')[0]}{{1}}}}{{}}\n" for s in figs), encoding="utf-8")
    return compile_pdf()


def compile_pdf() -> Path:
    cmd = ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "main.tex"]
    r = subprocess.run(cmd, cwd=PAPER, capture_output=True, text=True)
    if r.returncode != 0:   # stale auxiliary files after structural edits: clean once and retry
        subprocess.run(["latexmk", "-c"], cwd=PAPER, capture_output=True)
        r = subprocess.run(cmd, cwd=PAPER, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        raise RuntimeError("LaTeX build failed")
    zp = PAPER / "finverity_overleaf.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in [PAPER / "main.tex", PAPER / "references.bib", *GEN.glob("*.tex"), *FIGDIR.glob("*.pdf")]:
            if p.exists():
                z.write(p, p.relative_to(PAPER))
    print("built", PAPER / "main.pdf", "and", zp)
    return PAPER / "main.pdf"


if __name__ == "__main__":
    build()
