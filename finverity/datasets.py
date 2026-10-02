"""Financial transaction workload datasets -> common FinVerity transaction schema.

The datasets are used only as *financial transaction workloads*; fraud / laundering
labels are dropped. Audit evidence is generated separately (see evidence.py).

Common schema (all values are strings or ints so they canonicalize without floats):
    tx_id, source, seq, timestamp (ISO-8601 UTC), tx_type, amount (decimal string),
    currency, originator, beneficiary, orig_bal_before, orig_bal_after,
    dest_bal_before, dest_bal_after, channel, orig_location, dest_location
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_PROCESSED, DATA_RAW

SCHEMA = [
    "tx_id", "source", "seq", "timestamp", "tx_type", "amount", "currency",
    "originator", "beneficiary", "orig_bal_before", "orig_bal_after",
    "dest_bal_before", "dest_bal_after", "channel", "orig_location", "dest_location",
]

SOURCES = {
    "momtsim": DATA_RAW / "momtsim" / "MoMTSim_20240722202413_1000_dataset.csv",
    "saml_d": DATA_RAW / "saml_d" / "SAML-D.csv",
    "ibm_aml": DATA_RAW / "ibm_aml" / "HI-Small_Trans.csv",
}


def _money(s: pd.Series) -> pd.Series:
    return s.astype(float).map("{:.2f}".format)


def _spread_seconds(bucket: pd.Series, width_s: int) -> pd.Series:
    """Evenly spread rows sharing a coarse time bucket across that bucket, preserving order."""
    rank = bucket.groupby(bucket).cumcount()
    size = bucket.map(bucket.value_counts())
    return (rank * width_s // size).astype("int64")


def _momtsim(path: Path) -> pd.DataFrame:
    # MoMTSim (Azamuke et al., IEEE Access 2024): `step` = simulated hour, no tx id / wall clock.
    df = pd.read_csv(path, dtype={"initiator": str, "recipient": str})
    base = pd.Timestamp("2024-01-01T00:00:00Z")
    secs = df["step"].astype("int64") * 3600 + _spread_seconds(df["step"], 3600)
    ts = base + pd.to_timedelta(secs, unit="s")
    return pd.DataFrame({
        "tx_id": [f"MOM-{i:08d}" for i in range(len(df))],
        "timestamp": ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tx_type": df["transactionType"],
        "amount": _money(df["amount"]),
        "currency": "XXX",  # ISO 4217 "no currency": MoMTSim amounts are unitless
        "originator": df["initiator"],
        "beneficiary": df["recipient"],
        "orig_bal_before": _money(df["oldBalInitiator"]),
        "orig_bal_after": _money(df["newBalInitiator"]),
        "dest_bal_before": _money(df["oldBalRecipient"]),
        "dest_bal_after": _money(df["newBalRecipient"]),
        "channel": "MOBILE_MONEY",
        "orig_location": "",
        "dest_location": "",
    })


def _saml_d(path: Path) -> pd.DataFrame:
    # SAML-D (Oztas et al., 2023)
    df = pd.read_csv(path, dtype={"Sender_account": str, "Receiver_account": str})
    ts = pd.to_datetime(df["Date"] + "T" + df["Time"], utc=True)
    cur = {"UK pounds": "GBP", "Dirham": "AED", "Euro": "EUR", "US dollar": "USD", "Yen": "JPY",
           "Indian rupee": "INR", "Swiss franc": "CHF", "Pakistani rupee": "PKR", "Naira": "NGN",
           "Albanian lek": "ALL", "Turkish lira": "TRY", "Mexican Peso": "MXN", "Moroccan dirham": "MAD"}
    df = df.assign(_ts=ts).sort_values("_ts", kind="stable").reset_index(drop=True)
    return pd.DataFrame({
        "tx_id": [f"SAML-{i:08d}" for i in range(len(df))],
        "timestamp": df["_ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tx_type": df["Payment_type"].str.upper().str.replace(" ", "_").str.replace("-", "_"),
        "amount": _money(df["Amount"]),
        "currency": df["Payment_currency"].map(cur).fillna(df["Payment_currency"]),
        "originator": df["Sender_account"],
        "beneficiary": df["Receiver_account"],
        "orig_bal_before": "", "orig_bal_after": "", "dest_bal_before": "", "dest_bal_after": "",
        "channel": df["Payment_type"],
        "orig_location": df["Sender_bank_location"],
        "dest_location": df["Receiver_bank_location"],
    })


def _ibm_aml(path: Path) -> pd.DataFrame:
    # IBM AML (Altman et al., NeurIPS 2023), HI-Small. Two columns are both named "Account".
    df = pd.read_csv(path, dtype=str)
    df.columns = ["Timestamp", "From Bank", "From Account", "To Bank", "To Account",
                  "Amount Received", "Receiving Currency", "Amount Paid", "Payment Currency",
                  "Payment Format", "Is Laundering"]
    minute = pd.to_datetime(df["Timestamp"], format="%Y/%m/%d %H:%M", utc=True)
    df = df.assign(_m=minute).sort_values("_m", kind="stable").reset_index(drop=True)
    ts = df["_m"] + pd.to_timedelta(_spread_seconds(df["_m"].astype("int64"), 60), unit="s")
    return pd.DataFrame({
        "tx_id": [f"IBM-{i:08d}" for i in range(len(df))],
        "timestamp": ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tx_type": df["Payment Format"].str.upper().str.replace(" ", "_"),
        "amount": _money(df["Amount Paid"]),
        "currency": df["Payment Currency"],
        "originator": df["From Bank"] + ":" + df["From Account"],
        "beneficiary": df["To Bank"] + ":" + df["To Account"],
        "orig_bal_before": "", "orig_bal_after": "", "dest_bal_before": "", "dest_bal_after": "",
        "channel": df["Payment Format"],
        "orig_location": "BANK-" + df["From Bank"],
        "dest_location": "BANK-" + df["To Bank"],
    })


_LOADERS = {"momtsim": _momtsim, "saml_d": _saml_d, "ibm_aml": _ibm_aml}


def processed_path(source: str) -> Path:
    return DATA_PROCESSED / f"{source}.parquet"


def preprocess(source: str, force: bool = False) -> Path:
    out = processed_path(source)
    if out.exists() and not force:
        return out
    df = _LOADERS[source](SOURCES[source])
    df.insert(1, "source", source)
    df.insert(2, "seq", np.arange(len(df), dtype="int64"))
    df = df[SCHEMA]
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return out


def load_workload(source: str, n: int, seed: int) -> list[dict]:
    """Contiguous chronological window of n transactions starting at a seeded random offset.

    A contiguous window preserves the dataset's temporal ordering and account
    interactions, while the seeded offset gives each trial an independent sample.
    """
    df = pd.read_parquet(processed_path(source))
    if n > len(df):
        raise ValueError(f"{source} has only {len(df)} transactions")
    start = int(np.random.default_rng(seed).integers(0, len(df) - n + 1))
    window = df.iloc[start:start + n]
    return window.to_dict("records")


def profile(source: str) -> dict:
    df = pd.read_parquet(processed_path(source))
    amt = df["amount"].astype(float)
    return {
        "source": source,
        "transactions": len(df),
        "time_span": f"{df['timestamp'].iloc[0]} .. {df['timestamp'].iloc[-1]}",
        "tx_types": df["tx_type"].nunique(),
        "top_types": df["tx_type"].value_counts().head(5).to_dict(),
        "currencies": df["currency"].nunique(),
        "unique_originators": df["originator"].nunique(),
        "unique_beneficiaries": df["beneficiary"].nunique(),
        "amount_median": round(float(amt.median()), 2),
        "amount_p99": round(float(amt.quantile(0.99)), 2),
    }
