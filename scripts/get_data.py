"""Download the three transaction-workload datasets and verify them.

    python scripts/get_data.py            # all datasets
    python scripts/get_data.py momtsim    # only the primary dataset

The datasets are not redistributed with this repository; they are fetched from their
original publishers. MoMTSim files are checked against the SHA-256 digests published on
Mendeley Data. Kaggle archives are downloaded from the public dataset endpoint.
"""
from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

MOMTSIM = [  # Mendeley Data, doi:10.17632/zhj366m53p.2 (CC BY 4.0)
    ("MoMTSim_20240722202413_1000_dataset.csv",
     "https://data.mendeley.com/public-files/datasets/zhj366m53p/files/8a68b93f-5121-4510-bb6d-7862cd0776df/file_downloaded",
     "99fd07c3a9d3c4bd6d3462240058ca19d0d9e9284683f78bf77542ff7fcc05e7"),
    ("synthetic_mobile_money_transaction_dataset.csv",
     "https://data.mendeley.com/public-files/datasets/zhj366m53p/files/57c2ed32-a9b6-4af8-9510-9b064625d5d7/file_downloaded",
     "da951eb95735da96271740a3e66b676b342d3831ce3111cd19dbfa020d3bd0a7"),
]
KAGGLE = {
    "saml_d": ("berkanoztas/synthetic-transaction-monitoring-dataset-aml", ["SAML-D.csv"]),
    "ibm_aml": ("ealtman2019/ibm-transactions-for-anti-money-laundering-aml", ["HI-Small_Trans.csv", "HI-Small_accounts.csv"]),
}


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def momtsim() -> None:
    for name, url, digest in MOMTSIM:
        dest = RAW / "momtsim" / name
        if not dest.exists():
            print("downloading", name)
            _download(url, dest)
        ok = _sha256(dest) == digest
        print(f"{name}: SHA-256 {'OK' if ok else 'MISMATCH'}")
        if not ok:
            sys.exit(1)


def kaggle(key: str) -> None:
    slug, members = KAGGLE[key]
    folder = RAW / key
    archive = folder / "archive.zip"
    if not archive.exists():
        print("downloading", slug)
        _download(f"https://www.kaggle.com/api/v1/datasets/download/{slug}", archive)
    with zipfile.ZipFile(archive) as z:
        for m in members:
            if not (folder / m).exists():
                print("extracting", m)
                z.extract(m, folder)
    print(f"{key}: ready")


if __name__ == "__main__":
    which = sys.argv[1:] or ["momtsim", "saml_d", "ibm_aml"]
    for w in which:
        momtsim() if w == "momtsim" else kaggle(w)
