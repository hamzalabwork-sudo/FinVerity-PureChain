"""Experimental design (methodology section "Experimental Design").

E1  Controlled workloads W1-W5 on all three configurations      -> RQ2, RQ3, RQ4
E2  Offered-load sweep (saturation throughput, latency knee)     -> RQ2, RQ3
E3  Evidence-size sweep (10 KB .. 5 MB)                          -> RQ3, RQ4
E4  Evidence-complexity sweep (levels 1-4)                       -> RQ1, RQ3
E5  Validator-count sweep (Besu QBFT 4/7/10; PureChain fixed n=4)-> RQ3
E6  Integrity / tamper-detection experiment                      -> RQ1, RQ5
E7  Cross-dataset replication (W1, W2, W5 on SAML-D and IBM AML)  -> RQ2-RQ4
E8  Insider tampering (privileged operator rewrites commitments)  -> RQ1, RQ5
E9  Batched Merkle commitments (k = 1..100)                       -> RQ2-RQ4
"""
from __future__ import annotations

from finverity.config import EVIDENCE_SIZES as S

BACKENDS = ["purechain", "besu", "db_postgres"]
PUBLIC = "ethpos"
EMU = "poa2_local"  # PoA2 emulation calibrated to live PureChain (infra/poa2.py)   # conventional public-chain configuration (Ethereum PoS, mainnet timing)
BASE_RATE = 50.0

WORKLOADS = {
    #       n        evidence        trials  purpose
    "W1": (1_000, S["small"], 30),   # baseline
    "W2": (10_000, S["small"], 10),  # sustained workload
    "W3": (50_000, S["medium"], 5),  # scalability
    "W4": (100_000, S["medium"], 3), # high-volume
    "W5": (10_000, S["large"], 10),  # evidence-processing overhead
}


def plan(scale: str = "full") -> dict[str, list[dict]]:
    """Return experiment -> list of TrialConfig kwargs. scale='quick' is a smoke test."""
    q = scale == "quick"
    n_ = (lambda n: max(50, n // 100)) if q else (lambda n: n)
    t_ = (lambda t: min(t, 2)) if q else (lambda t: t)
    E = {k: [] for k in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9")}

    for w, (n, d, trials) in WORKLOADS.items():
        for b in BACKENDS:
            for t in range(t_(trials)):
                E["E1"].append(dict(workload=w, backend=b, n=n_(n), doc_bytes=d, level=4, rate=BASE_RATE, trial=t))

    for w, trials in (("W1", 5), ("W2", 2)):
        n, d, _ = WORKLOADS[w]
        for t in range(t_(trials)):
            E["E1"].append(dict(workload=w, backend=PUBLIC, n=n_(n), doc_bytes=d, level=4, rate=BASE_RATE, trial=t,
                                wait_timeout_s=3600))
    for rate in [10, 50]:
        for t in range(t_(2)):
            E["E2"].append(dict(workload="E2", backend=PUBLIC, n=n_(max(300, rate * 60)), doc_bytes=S["small"],
                                level=4, rate=rate, trial=t, wait_timeout_s=3600))

    for rate in [10, 25, 50, 100, 200, 400, 0]:
        for b in BACKENDS:
            for t in range(t_(5)):
                n = n_(max(2_000, int(rate * 30)) if rate else 5_000)
                E["E2"].append(dict(workload="E2", backend=b, n=n, doc_bytes=S["small"], level=4, rate=rate, trial=t))

    for size in ["none", "small", "medium", "large", "xlarge"]:
        for b in BACKENDS:
            for t in range(t_(5)):
                E["E3"].append(dict(workload="E3", backend=b, n=n_(1_000), doc_bytes=S[size], level=4,
                                    rate=BASE_RATE, trial=t, tag=size))

    for level in [1, 2, 3, 4]:
        for b in BACKENDS:
            for t in range(t_(5)):
                E["E4"].append(dict(workload="E4", backend=b, n=n_(1_000), doc_bytes=S["medium"], level=level,
                                    rate=BASE_RATE, trial=t))

    for v in [4, 7, 10]:
        for rate in [50, 200]:
            for t in range(t_(5)):
                E["E5"].append(dict(workload="E5", backend="besu", n=n_(3_000), doc_bytes=S["small"], level=4,
                                    rate=rate, trial=t, tag=f"v{v}", validators=v))

    for src in ["momtsim", "saml_d", "ibm_aml"]:
        for t in range(t_(3)):
            E["E6"].append(dict(workload="E6", backend="purechain", source=src, n=n_(1_000), doc_bytes=S["medium"],
                                level=4, rate=BASE_RATE, trial=t, keep_store=True, verify_n=n_(1_000)))

    # E7 cross-dataset replication: the core workloads repeated on the two secondary datasets
    # (MoMTSim results come from E1), plus the insider experiment on every dataset.
    for src in ["saml_d", "ibm_aml"]:
        for w, trials in (("W1", 10), ("W2", 5), ("W5", 3)):
            n, d, _ = WORKLOADS[w]
            for b in BACKENDS:
                for t in range(t_(trials)):
                    E["E7"].append(dict(workload="E7", backend=b, source=src, n=n_(n), doc_bytes=d, level=4,
                                        rate=BASE_RATE, trial=t, tag=w))
        for b in BACKENDS:
            for t in range(t_(2)):
                E["E8"].append(dict(workload="E8", backend=b, source=src, n=n_(300), doc_bytes=S["small"], level=4,
                                    rate=20, trial=t, verify_n=n_(300), insider=n_(100)))
    for b in BACKENDS + [PUBLIC]:
        for t in range(t_(3 if b != PUBLIC else 2)):
            E["E8"].append(dict(workload="E8", backend=b, n=n_(300), doc_bytes=S["small"], level=4, rate=20,
                                trial=t, verify_n=n_(300), insider=n_(100)))

    for k in [1, 50]:
        for t in range(t_(2)):
            E["E9"].append(dict(workload="E9", backend=PUBLIC, n=n_(3_000), doc_bytes=S["small"], level=4, rate=50,
                                trial=t, batch_size=k, batch_wait_s=1.0, wait_timeout_s=3600))
    for k in [1, 10, 25, 50, 100]:
        for rate in [50, 200]:
            for b in BACKENDS:
                for t in range(t_(5)):
                    E["E9"].append(dict(workload="E9", backend=b, n=n_(max(3_000, rate * 30)), doc_bytes=S["small"],
                                        level=4, rate=rate, trial=t, batch_size=k, batch_wait_s=1.0))
    # E5: the emulation can also vary its signer count (live PureChain is fixed at 4 validators)
    for v in [4, 7, 10]:
        for rate in [50, 200]:
            for t in range(t_(5)):
                E["E5"].append(dict(workload="E5", backend=EMU, n=n_(3_000), doc_bytes=S["small"], level=4,
                                    rate=rate, trial=t, tag=f"v{v}", validators=v))
    # every live-PureChain trial gets an identical twin on the PoA2 emulation
    for k in E:
        E[k] += [{**c, "backend": EMU} for c in E[k] if c["backend"] == "purechain"]
    return E
