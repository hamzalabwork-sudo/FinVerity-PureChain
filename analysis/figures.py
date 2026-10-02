"""Publication figures (PDF + 300 dpi PNG) -> results/figures/.

Palette: validated categorical slots 1-3 (all-pairs CVD dE >= 9.2, normal-vision dE >= 24).
The aqua slot is < 3:1 on white, so every series also carries a distinct marker/line style
and a direct label (identity is never color-alone; survives grayscale print).

    python -m analysis.figures
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from finverity.config import RESULTS
from .stats import RAW, load_summaries

FIG = RESULTS / "figures"
ORDER = ["purechain", "poa2_local", "besu", "db_postgres", "ethpos"]
LABEL = {"purechain": "FinVerity (PureChain PoA², live)", "poa2_local": "FinVerity (PoA² emulation)", "besu": "Besu QBFT", "db_postgres": "PostgreSQL",
         "ethpos": "Ethereum PoS (public-chain config.)"}
SHORT = {"purechain": "PureChain (live)", "poa2_local": "PoA² emulation", "besu": "Besu QBFT", "db_postgres": "PostgreSQL", "ethpos": "Ethereum PoS"}
# validated categorical slots 1-4 (adjacent-pair CVD dE >= 9.1); slots 3-4 < 3:1 -> markers + direct labels
COLOR = {"purechain": "#2a78d6", "poa2_local": "#2a78d6", "besu": "#eb6834", "db_postgres": "#1baf7a", "ethpos": "#eda100"}
MARKER = {"purechain": "o", "poa2_local": "P", "besu": "s", "db_postgres": "^", "ethpos": "D"}
LINE = {"purechain": "-", "poa2_local": "-", "besu": "--", "db_postgres": ":", "ethpos": "-."}
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "font.size": 8.5, "axes.titlesize": 9, "axes.labelsize": 8.5, "legend.fontsize": 7.5,
    "xtick.labelsize": 7.5, "ytick.labelsize": 7.5, "axes.edgecolor": INK2, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "axes.spines.top": False,
    "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True, "lines.linewidth": 1.6, "lines.markersize": 5, "legend.frameon": False,
    "savefig.bbox": "tight", "pdf.fonttype": 42, "figure.dpi": 150,
})


def _save(fig, name):
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / f"{name}.pdf")
    fig.savefig(FIG / f"{name}.png", dpi=300)
    plt.close(fig)
    print("  wrote", name)


def _backends(df):
    return [b for b in ORDER if b in set(df["backend"])]


def _series(ax, x, y, b, lo=None, hi=None, label=True):
    ax.plot(x, y, LINE[b], color=COLOR[b], marker=MARKER[b], markeredgecolor="white", markeredgewidth=0.8,
            label=LABEL[b])
    if lo is not None:
        ax.fill_between(x, lo, hi, color=COLOR[b], alpha=0.12, linewidth=0)


def _direct_label(ax, x, y, b, dx=4):
    if not len(x):
        return
    placed = getattr(ax, "_label_y", [])
    yy = ax.transData.transform((x[-1], y[-1]))[1] * 72 / ax.figure.dpi   # label anchor in points
    dy = 0
    while any(abs(yy + dy - p) < 8.5 for p in placed):   # keep >= 8.5 pt between direct labels
        dy -= 8.5
    placed.append(yy + dy)
    ax._label_y = placed
    ax.annotate(SHORT[b], (x[-1], y[-1]), xytext=(dx, dy), textcoords="offset points",
                va="center", fontsize=7, color=INK2)


def _lat(run_ids):
    fr = [pd.read_parquet(RAW / r / "items.parquet", columns=["status", "commit_latency_s"]) for r in run_ids]
    if not fr:
        return np.array([])
    d = pd.concat(fr)
    return d.loc[d["status"] == "committed", "commit_latency_s"].dropna().to_numpy()


def _agg(g, col):
    x = g[col].astype(float)
    m = x.mean()
    h = 1.96 * x.std(ddof=1) / np.sqrt(len(x)) if len(x) > 1 else 0.0
    return m, m - h, m + h


# ---------------------------------------------------------------- F1 latency CDF
def fig_latency_cdf(df):
    sub = df[df["workload"] == "W2"]
    if not len(sub):
        sub = df[df["workload"].str.match(r"W\d")]
    if not len(sub):
        return
    fig, ax = plt.subplots(figsize=(3.4, 2.4))
    for b in _backends(sub):
        lat = np.sort(_lat(sub.loc[sub["backend"] == b, "run_id"]))
        if not len(lat):
            continue
        y = np.arange(1, len(lat) + 1) / len(lat)
        ax.plot(lat, y, LINE[b], color=COLOR[b], label=LABEL[b])
        i = int(0.5 * len(lat))
        ax.plot(lat[i], y[i], MARKER[b], color=COLOR[b], markeredgecolor="white")
    ax.set_xscale("log")
    ax.set_xlabel("Commit latency (s, log scale)")
    ax.set_ylabel("Fraction of records")
    ax.axhline(0.95, color=INK2, lw=0.6, ls=(0, (2, 2)))
    ax.text(ax.get_xlim()[0] * 1.1, 0.955, "P95", fontsize=6.5, color=INK2, va="bottom")
    ax.set_title(f"Commit-latency distribution ({sub['workload'].iloc[0]})", loc="left")
    ax.legend(loc="lower right")
    _save(fig, "F1_latency_cdf")


# ---------------------------------------------------------------- F2 offered-load sweep
def fig_load_sweep(df):
    sub = df[df["workload"] == "E2"].copy()
    if not len(sub):
        return
    maxr = sub.loc[sub["rate"] > 0, "rate"].max()
    sub["x"] = np.where(sub["rate"] > 0, sub["rate"], maxr * 2)
    fig, axs = plt.subplots(1, 2, figsize=(7, 2.6))
    for b in _backends(sub):
        g = sub[sub["backend"] == b].groupby("x")
        xs = np.array(sorted(g.groups))
        th = np.array([_agg(g.get_group(x), "throughput_tps") for x in xs])
        p50 = np.array([_agg(g.get_group(x), "commit_latency_s_median") for x in xs])
        p95 = np.array([_agg(g.get_group(x), "commit_latency_s_p95") for x in xs])
        _series(axs[0], xs, th[:, 0], b, th[:, 1], th[:, 2])
        _direct_label(axs[0], xs, th[:, 0], b)
        _series(axs[1], xs, p50[:, 0], b)
        axs[1].plot(xs, p95[:, 0], LINE[b], color=COLOR[b], alpha=0.45, lw=1.0)
        _direct_label(axs[1], xs, p50[:, 0], b)
    lim = [1, maxr * 2.6]
    axs[0].plot(lim, lim, color=INK2, lw=0.6, ls=(0, (2, 2)), label="ideal (throughput = offered)")
    for ax in axs:
        ax.set_xscale("log")
        ticks = sorted(sub["x"].unique())
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t:g}" if t <= maxr else "max" for t in ticks])
        ax.set_xlabel("Offered load (tx/s)")
    axs[0].set_yscale("log"); axs[0].set_ylabel("Achieved throughput (tx/s)")
    axs[0].set_title("Throughput vs offered load", loc="left")
    axs[1].set_yscale("log"); axs[1].set_ylabel("Commit latency (s)")
    axs[1].set_title("Latency vs offered load (solid P50, faint P95)", loc="left")
    axs[0].legend(loc="upper left", fontsize=6.3)
    fig.tight_layout()
    _save(fig, "F2_offered_load_sweep")


# ---------------------------------------------------------------- F3 workloads W1-W5
def fig_workloads(df):
    sub = df[df["workload"].str.match(r"W\d")]
    if not len(sub):
        return
    ws = sorted(sub["workload"].unique())
    fig, axs = plt.subplots(1, 3, figsize=(7, 2.4), sharey=False)
    for ax, col, title in zip(axs, ["commit_latency_s_median", "commit_latency_s_p95", "throughput_tps"],
                              ["Median commit latency (s)", "P95 commit latency (s)", "Throughput (tx/s)"]):
        for b in _backends(sub):
            g = sub[sub["backend"] == b].groupby("workload")
            xs = [w for w in ws if w in g.groups]
            v = np.array([_agg(g.get_group(w), col) for w in xs])
            xi = np.array([ws.index(w) for w in xs])
            ax.errorbar(xi, v[:, 0], yerr=[v[:, 0] - v[:, 1], v[:, 2] - v[:, 0]], fmt=MARKER[b] + LINE[b],
                        color=COLOR[b], markeredgecolor="white", capsize=2, elinewidth=0.8, label=LABEL[b])
        ax.set_xticks(range(len(ws))); ax.set_xticklabels(ws)
        ax.set_title(title, loc="left")
        if col != "throughput_tps":
            ax.set_yscale("log")
    h, l = axs[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, -0.12), ncol=len(l), fontsize=6.5, frameon=False)
    fig.text(0.5, -0.02, "Workload (W1 1k/10 KB · W2 10k/10 KB · W3 50k/100 KB · W4 100k/100 KB · W5 10k/1 MB); "
             "error bars: 95% CI over trials", ha="center", fontsize=6.5, color=INK2)
    fig.tight_layout()
    _save(fig, "F3_workloads")


# ---------------------------------------------------------------- F4 evidence size
def fig_evidence_size(df):
    sub = df[df["workload"] == "E3"].copy()
    if not len(sub):
        return
    sub["kb"] = sub["doc_bytes"].replace(0, 1024 * 0.5) / 1024
    fig, axs = plt.subplots(1, 2, figsize=(7, 2.5))
    for b in _backends(sub):
        g = sub[sub["backend"] == b].groupby("kb")
        xs = np.array(sorted(g.groups))
        prep = np.array([_agg(g.get_group(x).assign(p=lambda d: 1e3 * (d["prep_evidence_s_mean"] + d["prep_store_s_mean"])), "p") for x in xs])
        lat = np.array([_agg(g.get_group(x), "commit_latency_s_median") for x in xs])
        _series(axs[0], xs, prep[:, 0], b, prep[:, 1], prep[:, 2]); _direct_label(axs[0], xs, prep[:, 0], b)
        _series(axs[1], xs, lat[:, 0], b, lat[:, 1], lat[:, 2]); _direct_label(axs[1], xs, lat[:, 0], b)
    for ax in axs:
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_xlabel("Document size per record (KB; leftmost = none)")
    axs[0].set_ylabel("Client evidence processing (ms/record)")
    axs[0].set_title("Evidence generation + hashing + off-chain store", loc="left")
    axs[1].set_ylabel("Median commit latency (s)")
    axs[1].set_title("Commit latency is independent of evidence size", loc="left")
    fig.tight_layout()
    _save(fig, "F4_evidence_size")


# ---------------------------------------------------------------- F5 complexity
def fig_complexity(df):
    sub = df[df["workload"] == "E4"]
    if not len(sub):
        return
    fig, axs = plt.subplots(1, 2, figsize=(7, 2.4))
    for b in _backends(sub):
        g = sub[sub["backend"] == b].groupby("level")
        xs = np.array(sorted(g.groups))
        pkg = np.array([_agg(g.get_group(x).assign(p=lambda d: 1e3 * d["prep_evidence_s_mean"]), "p") for x in xs])
        lat = np.array([_agg(g.get_group(x), "commit_latency_s_median") for x in xs])
        _series(axs[0], xs, pkg[:, 0], b, pkg[:, 1], pkg[:, 2]); _direct_label(axs[0], xs, pkg[:, 0], b)
        _series(axs[1], xs, lat[:, 0], b, lat[:, 1], lat[:, 2]); _direct_label(axs[1], xs, lat[:, 0], b)
    for ax in axs:
        ax.set_xticks([1, 2, 3, 4]); ax.set_xticklabels(["L1\nmetadata", "L2\n+invoice", "L3\n+authoriz.", "L4\ncomplete"])
    axs[0].set_ylabel("Evidence generation + hash (ms)"); axs[0].set_title("Evidence complexity cost (client)", loc="left")
    axs[1].set_ylabel("Median commit latency (s)"); axs[1].set_yscale("log")
    axs[1].set_title("Commit latency by complexity level", loc="left")
    fig.tight_layout()
    _save(fig, "F5_complexity")


# ---------------------------------------------------------------- F6 validators
def fig_validators(df):
    sub = df[(df["workload"] == "E5") & ~df["tag"].fillna("").str.contains("confirm")].copy()
    if not len(sub):
        return
    sub["v"] = sub["tag"].str.extract(r"v(\d+)")[0].astype(int)
    fig, axs = plt.subplots(1, 2, figsize=(7, 2.5))
    for b in _backends(sub):
        for rate in sorted(sub["rate"].unique()):
            g = sub[(sub["backend"] == b) & (sub["rate"] == rate)].groupby("v")
            xs = np.array(sorted(g.groups))
            if not len(xs):
                continue
            th = np.array([_agg(g.get_group(x), "throughput_tps") for x in xs])
            ok = np.array([g.get_group(x)["success_rate"].mean() for x in xs])
            lat = np.array([g.get_group(x)["e2e_latency_s_median"].mean() for x in xs])
            ls = "-" if rate == sub["rate"].min() else "--"
            for ax, v in ((axs[0], th[:, 0]), (axs[1], lat)):
                ax.plot(xs, v, ls, color=COLOR[b], marker=MARKER[b], markeredgecolor="white",
                        label=f"{SHORT[b]} @ {rate:g} rec/s")
                _lbl = f"{SHORT[b]} @ {rate:g}"
                ax.annotate(_lbl, (xs[-1], v[-1]), xytext=(4, 0), textcoords="offset points", fontsize=6.3,
                            color=INK2, va="center")
            for x, o, t in zip(xs, ok, th[:, 0]):
                if o < 0.99:
                    axs[0].annotate(f"{100 * o:.0f}% committed", (x, t), xytext=(-6, 10), textcoords="offset points",
                                    fontsize=6.3, color=INK2, ha="right",
                                    arrowprops=dict(arrowstyle="-", color=INK2, lw=0.5))
    for ax in axs:
        ax.set_xticks(sorted(sub["v"].unique())); ax.set_xlabel("Number of validators (1 CPU each)")
        ax.set_xlim(3.5, 12.5)
    axs[0].set_ylabel("Throughput (records/s)"); axs[0].set_title("Throughput vs validator count", loc="left")
    axs[1].set_yscale("log"); axs[1].set_ylabel("Median inclusion latency (s)")
    axs[1].set_title("Latency vs validator count", loc="left")
    fig.tight_layout()
    _save(fig, "F6_validators")


# ---------------------------------------------------------------- F7 energy & cost
def fig_energy_cost(df):
    sub = df[df["workload"] == "W2"]
    if not len(sub):
        sub = df[df["workload"].str.match(r"W\d")]
    if not len(sub):
        return
    from matplotlib.ticker import LogLocator, LogFormatterMathtext, NullFormatter
    bs = _backends(sub)
    y = np.arange(len(bs))
    fig, axs = plt.subplots(1, 3, figsize=(7.4, 2.3))
    panels = ((axs[0], "energy_j_per_tx", "Attributed energy (J/record)", 1.0),
              (axs[1], "energy_dynamic_j_per_tx", "Measured dynamic energy (J/record)", 1.0),
              (axs[2], "cost_total_usd_per_tx", "Total cost per record (USD, incl. fees)", 1.0))
    for ax, col, title, scale in panels:
        vals = [_agg(sub[sub["backend"] == b], col) for b in bs]
        for i, (b, v) in enumerate(zip(bs, vals)):
            ax.barh(i, v[0] * scale, color=COLOR[b], height=0.6, edgecolor="white", linewidth=2)
            txt = f"{v[0]:.3g}" if col != "cost_total_usd_per_tx" else (f"${v[0]:.2f}" if v[0] >= 0.01 else f"${v[0]:.1e}")
            if col == "energy_dynamic_j_per_tx" and b == "purechain":
                txt += " (client only)"
            ax.text(v[0] * scale * 1.15, i, txt, va="center", fontsize=6.8, color=INK)
        ax.set_yticks(y); ax.set_yticklabels([SHORT[b] for b in bs] if ax is axs[0] else [""] * len(bs))
        ax.invert_yaxis(); ax.set_xscale("log"); ax.grid(axis="y", visible=False)
        ax.xaxis.set_major_locator(LogLocator(base=10, numticks=6))
        ax.xaxis.set_major_formatter(LogFormatterMathtext()); ax.xaxis.set_minor_formatter(NullFormatter())
        lo = min(v[0] for v in vals if v[0] > 0)
        hi = max(v[0] for v in vals)
        ax.set_xlim(lo / 3, hi * 30)
        ax.set_title(title, loc="left", fontsize=8)
    fig.tight_layout()
    _save(fig, "F7_energy_cost")


# ---------------------------------------------------------------- F8 verification breakdown
def fig_verification(df):
    sub = df[df["workload"].str.match(r"W\d")]
    if not len(sub):
        return
    bs = _backends(sub)
    rows = []
    for b in bs:
        fr = [pd.read_parquet(RAW / r / "verify.parquet") for r in sub.loc[sub["backend"] == b, "run_id"]
              if (RAW / r / "verify.parquet").exists()]
        fr = [f for f in fr if len(f)]
        if fr:
            v = pd.concat(fr)
            rows.append((b, 1e3 * v["chain_fetch_s"].median(), 1e3 * v["store_fetch_s"].median(),
                         1e3 * v["rehash_s"].median(), v["match"].mean(), len(v)))
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(3.6, 1.9))
    parts = [("record lookup", "#1c5cab"), ("evidence retrieval", "#5598e7"), ("re-hash + compare", "#b7d3f6")]
    for i, (b, *vals, ok, n) in enumerate(rows):
        left = 0
        for (name, c), v in zip(parts, vals):
            ax.barh(i, v, left=left, color=c, height=0.6, edgecolor="white", linewidth=2, label=name if i == 0 else None)
            left += v
        ax.text(left, i, f"  {left:.2f} ms · {100 * ok:.1f}% verified (n={n})", va="center", fontsize=6.5)
    ax.set_yticks(range(len(rows))); ax.set_yticklabels([SHORT[r[0]] for r in rows]); ax.invert_yaxis()
    ax.set_xlabel("Median verification time per record (ms)"); ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(sum(r[1:4]) for r in rows) * 2.2)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=3, fontsize=6.5, frameon=False)
    ax.set_title("Continuous evidence verification", loc="left")
    _save(fig, "F8_verification")


# ---------------------------------------------------------------- F9 finality ladder
def fig_finality(df):
    sub = df[df["workload"] == "W2"]
    if not len(sub):
        sub = df[df["workload"].str.match(r"W\d")]
    if not len(sub):
        return
    stages = [("ack_latency_s", "Submission ack"), ("commit_latency_s", "Canonical inclusion"),
              ("conf1_latency_s", "1 confirmation"), ("conf3_latency_s", "3 confirmations"),
              ("final_latency_s", "Finality (backend rule)")]
    bs = [b for b in _backends(sub) if b != "db_postgres"]
    fig, ax = plt.subplots(figsize=(3.6, 2.3))
    off = {b: (i - (len(bs) - 1) / 2) * 0.22 for i, b in enumerate(bs)}
    for b in bs:
        g = sub[sub["backend"] == b]
        med = [g[f"{c}_median"].mean() for c, _ in stages]
        p95 = [g[f"{c}_p95"].mean() for c, _ in stages]
        y = np.arange(len(stages)) + off[b]
        ax.hlines(y, med, p95, color=COLOR[b], lw=1.4)
        ax.plot(med, y, MARKER[b], color=COLOR[b], markeredgecolor="white", label=f"{SHORT[b]} (marker median, line to P95)")
    if "db_postgres" in set(sub["backend"]):
        v = sub.loc[sub["backend"] == "db_postgres", "commit_latency_s_median"].mean()
        ax.axvline(v, color=COLOR["db_postgres"], ls=":", lw=1.2)
        ax.text(v * 1.15, len(stages) - 0.55, f"PostgreSQL commit {1e3 * v:.0f} ms", fontsize=6.5, color=INK2)
    ax.set_yticks(range(len(stages))); ax.set_yticklabels([n for _, n in stages]); ax.invert_yaxis()
    ax.set_xscale("log"); ax.set_xlabel("Latency from submission (s, log scale)")
    ax.grid(axis="y", visible=False)
    ax.set_title("Inclusion vs confirmation-depth finality", loc="left")
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.22), fontsize=6.3, ncol=1, frameon=False)
    _save(fig, "F9_finality_ladder")


# ---------------------------------------------------------------- F10 batching
def fig_batching(df):
    sub = df[df["workload"] == "E9"]
    if not len(sub):
        return
    fig, axs = plt.subplots(1, 3, figsize=(7.2, 2.4))
    rate = sub["rate"].max()
    g0 = sub[sub["rate"] == rate]
    for b in _backends(g0):
        g = g0[g0["backend"] == b].groupby("batch_size")
        xs = np.array(sorted(g.groups))
        for ax, col in ((axs[0], "throughput_tps"), (axs[1], "final_e2e_latency_s_median"), (axs[2], "gas_used_mean")):
            if col == "gas_used_mean" and b == "db_postgres":
                continue
            v = np.array([_agg(g.get_group(x), col) for x in xs])
            _series(ax, xs, v[:, 0], b, v[:, 1], v[:, 2])
            if ax is not axs[0]:
                _direct_label(ax, xs, v[:, 0], b)
    axs[0].legend(loc="lower right", fontsize=6.3)
    for ax in axs:
        ax.set_xscale("log"); ax.set_xlabel("Records per commitment k")
    axs[0].axhline(rate, color=INK2, lw=0.6, ls=(0, (2, 2)))
    axs[0].set_ylabel("Records committed per second"); axs[0].set_title(f"Throughput @ {rate:g} rec/s offered", loc="left")
    axs[1].set_yscale("log"); axs[1].set_ylabel("Arrival -> finality (s)"); axs[1].set_title("End-to-end finality latency", loc="left")
    axs[2].set_yscale("log"); axs[2].set_ylabel("Gas per record"); axs[2].set_title("On-chain cost per record", loc="left")
    fig.tight_layout()
    _save(fig, "F10_batching")


# ---------------------------------------------------------------- F11 insider tampering
def fig_insider(df):
    frames = [pd.read_parquet(p).assign(run_id=p.parent.name) for p in RAW.glob("*/insider.parquet")]
    if not frames:
        return
    d = pd.concat(frames)
    d["backend"] = d["run_id"].map(df.set_index("run_id")["backend"])
    d = d.dropna(subset=["backend"])
    scen = [("S1_evidence_only", "Alter evidence"), ("S2_commitment_rewrite", "Alter evidence +\nrewrite commitment"),
            ("S3_backdated_record", "Fabricate +\nbackdate record")]
    bs = _backends(d)
    fig, ax = plt.subplots(figsize=(4.2, 2.3))
    w = 0.8 / len(bs)
    for i, b in enumerate(bs):
        vals = []
        for sc, _ in scen:
            g = d[(d["backend"] == b) & (d["scenario"] == sc)]
            vals.append(100 * g["outcome"].isin(["prevented", "detected"]).mean() if len(g) else np.nan)
        x = np.arange(len(scen)) + (i - (len(bs) - 1) / 2) * w
        ax.bar(x, vals, width=w, color=COLOR[b], edgecolor="white", linewidth=2, label=SHORT[b])
        for xi, v in zip(x, vals):
            if v == v:
                ax.text(xi, v + 2, f"{v:.0f}", ha="center", fontsize=6.5, color=INK)
    ax.set_xticks(range(len(scen))); ax.set_xticklabels([n for _, n in scen])
    ax.set_ylim(0, 115); ax.set_ylabel("Attacks prevented or detected (%)")
    ax.set_title("Privileged-insider tampering", loc="left"); ax.grid(axis="x", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), fontsize=6.5, ncol=len(bs), frameon=False)
    _save(fig, "F11_insider")


def main():
    df = load_summaries()
    if not len(df):
        print("no results")
        return
    for f in (fig_latency_cdf, fig_load_sweep, fig_workloads, fig_evidence_size, fig_complexity,
              fig_validators, fig_energy_cost, fig_verification, fig_finality, fig_batching, fig_insider):
        try:
            f(df)
        except Exception as e:
            print(f"  {f.__name__} failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
