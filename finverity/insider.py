"""Insider-tampering experiment (E8).

Threat model: a privileged insider (system operator / DBA) who holds the submitter and approver
credentials, i.e. has full write access to the system of record, tries to make altered evidence
pass the audit. The auditor re-derives each record ID from the transaction ID, recomputes the
evidence hash and checks the recorded commitment and its recording time.

    S1 evidence_only       alter the off-chain evidence, leave the commitment
    S2 commitment_rewrite  alter the evidence AND overwrite the stored commitment with H(E')
    S3 backdated_record    fabricate a replacement record for the transaction and claim it was
                           recorded at the original time

Outcomes: prevented (the write was rejected), detected (audit fails), undetected (audit passes).
"""
from __future__ import annotations

import copy
import time

from . import evidence
from .backends.base import Item, sign_approval
from .canonical import sha256

TOLERANCE_S = 30.0   # recording time may lag the claimed time by at most this much


def _tamper(pkg: dict) -> dict:
    p = copy.deepcopy(pkg)
    whole, _, frac = p["metadata"]["amount"].partition(".")
    p["metadata"]["amount"] = f"{int(whole) + 1000}.{frac or '00'}"
    return p


def _audit(backend, record_id: bytes, pkg: dict, docs: dict, claimed_ts: float) -> str:
    rec = backend.fetch_record(record_id)
    if rec is None:
        return "detected"                      # commitment missing for a known transaction
    if rec["evidence_hash"] != evidence.recompute(pkg, docs):
        return "detected"
    if rec["timestamp"] > claimed_ts + TOLERANCE_S:
        return "detected"                      # recorded later than claimed
    return "undetected"


def _write_evm(backend, submitter, approver, record_id: bytes, h: bytes, meta: bytes, level: int) -> bool:
    it = Item(0, "insider", record_id, h, meta, level, 0, 0, 0)
    it.approval = sign_approval(approver, backend.chain.chain_id, backend.contract.address, it)
    data = backend._calldata(it)
    try:
        backend._send_and_wait(submitter, {"to": backend.contract.address, "data": data, "gas": 400_000})
        return True
    except Exception:
        return False


def _write_db(backend, record_id: bytes, h: bytes, recorded_at: float, new: bool, meta: bytes, level: int,
              submitter: str, approver: str) -> bool:
    con = backend._con()
    ph = backend.ph
    try:
        if new:
            con.execute(f"INSERT INTO evidence_records VALUES({','.join([ph] * 8)})",
                        (record_id, h, meta, submitter, approver, level, b"", recorded_at))
        else:
            con.execute(f"UPDATE evidence_records SET evidence_hash={ph} WHERE record_id={ph}", (h, record_id))
        con.commit()
        return True
    except Exception:
        con.rollback()
        return False


def run_insider(backend, store, records: list[dict], submitter, approver, run_start_wall: float) -> list[dict]:
    """records: [{'record_id', 'tx_id', 'recorded_ts'}] of committed per-record evidence."""
    evm = backend.kind == "blockchain"
    rows = []
    for r in records:
        rid = r["record_id"]
        pkg, docs = store.get(rid)
        bad = _tamper(pkg)
        h_bad = evidence.recompute(bad, docs)
        meta = sha256(b"insider")

        # S1: evidence altered, commitment untouched
        rows.append({"scenario": "S1_evidence_only", "record_id": rid.hex(), "write_accepted": None,
                     "outcome": _audit(backend, rid, bad, docs, r["recorded_ts"])})

        # S2: overwrite the stored commitment with H(E')
        ok = (_write_evm(backend, submitter, approver, rid, h_bad, meta, 4) if evm else
              _write_db(backend, rid, h_bad, 0, False, meta, 4, submitter.address, approver.address))
        out = _audit(backend, rid, bad, docs, r["recorded_ts"])
        rows.append({"scenario": "S2_commitment_rewrite", "record_id": rid.hex(), "write_accepted": ok,
                     "outcome": "prevented" if not ok and out == "detected" else out})

        # S3: fabricate a replacement record and claim the original recording time
        rid2 = sha256(b"replacement|" + rid)
        ok = (_write_evm(backend, submitter, approver, rid2, h_bad, meta, 4) if evm else
              _write_db(backend, rid2, h_bad, r["recorded_ts"], True, meta, 4, submitter.address, approver.address))
        out = _audit(backend, rid2, bad, docs, r["recorded_ts"]) if ok else "prevented"
        rows.append({"scenario": "S3_backdated_record", "record_id": rid.hex(), "write_accepted": ok,
                     "outcome": out})
    return rows
