"""Integrity experiment: controlled evidence modifications and tamper detection rate.

Each committed record is subjected to every applicable modification class; the modified
evidence E_i' is verified against the on-chain commitment H_i. Detection means
H(E_i') != H_i. Benign transformations (re-serialization, key reordering, Unicode
normalization form) must NOT be flagged - they measure the false-positive rate and
validate the canonicalization step.

    TDR = detected / applicable x 100        FPR = flagged benign / benign x 100
"""
from __future__ import annotations

import copy
import json
import time
import unicodedata

import numpy as np

from . import evidence


def _bump_amount(s: str) -> str:
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2]) + 1
    return f"{cents // 100}.{cents % 100:02d}"


def _next_day(ts: str) -> str:
    from datetime import datetime, timedelta
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in ts else "%Y-%m-%d"
    return (datetime.strptime(ts, fmt) + timedelta(days=1)).strftime(fmt)


def _flip_bit_in_string(s: str, rng) -> str:
    if not s:
        return "x"
    i = int(rng.integers(0, len(s)))
    c = chr(ord(s[i]) ^ 1)
    return s[:i] + c + s[i + 1:]


# name -> (applicable(pkg, docs), mutate(pkg, docs, rng, ctx) -> (pkg', docs'))
def _m_amount(p, d, rng, ctx):
    p["metadata"]["amount"] = _bump_amount(p["metadata"]["amount"]); return p, d

def _m_date(p, d, rng, ctx):
    p["metadata"]["timestamp"] = _next_day(p["metadata"]["timestamp"]); return p, d

def _m_invoice_no(p, d, rng, ctx):
    head, _, num = p["invoice"]["invoice_no"].rpartition("-")
    p["invoice"]["invoice_no"] = f"{head}-{(int(num) + 1):0{len(num)}d}"; return p, d

def _m_vendor(p, d, rng, ctx):
    p["invoice"]["vendor"]["name"] = p["invoice"]["vendor"]["name"] + " Holdings"; return p, d

def _m_authorization(p, d, rng, ctx):
    others = [a for a in ctx["approvers"] if a != p["attestation"]["approver"]]
    new = others[int(rng.integers(0, len(others)))]
    p["attestation"]["approver"] = new
    if "authorization" in p:
        p["authorization"]["approver"] = new
    return p, d

def _m_document(p, d, rng, ctx):
    ref = p["documents"][int(rng.integers(0, len(p["documents"])))]
    blob = bytearray(d[ref["doc_id"]])
    pos = int(rng.integers(0, len(blob)))
    blob[pos] ^= 0x01
    d[ref["doc_id"]] = bytes(blob); return p, d

def _m_document_deleted(p, d, rng, ctx):
    ref = p["documents"][int(rng.integers(0, len(p["documents"])))]
    d.pop(ref["doc_id"], None); return p, d

def _m_ledger(p, d, rng, ctx):
    e = p["ledger"]["entries"]
    e[0]["account"], e[1]["account"] = e[1]["account"], e[0]["account"]; return p, d

def _m_beneficiary(p, d, rng, ctx):
    p["metadata"]["beneficiary"] = _flip_bit_in_string(p["metadata"]["beneficiary"], rng); return p, d

def _m_single_bit(p, d, rng, ctx):
    p["metadata"]["originator"] = _flip_bit_in_string(p["metadata"]["originator"], rng); return p, d

def _m_substitution(p, d, rng, ctx):
    return copy.deepcopy(ctx["other_pkg"]), dict(ctx["other_docs"])


MODIFICATIONS = {
    "amount": (lambda p, d: True, _m_amount),
    "date": (lambda p, d: True, _m_date),
    "invoice_number": (lambda p, d: "invoice" in p, _m_invoice_no),
    "vendor": (lambda p, d: "invoice" in p, _m_vendor),
    "authorization": (lambda p, d: True, _m_authorization),
    "supporting_document": (lambda p, d: bool(p.get("documents")), _m_document),
    "document_deleted": (lambda p, d: bool(p.get("documents")), _m_document_deleted),
    "ledger_entry": (lambda p, d: "ledger" in p, _m_ledger),
    "beneficiary": (lambda p, d: True, _m_beneficiary),
    "single_bit_metadata": (lambda p, d: True, _m_single_bit),
    "record_substitution": (lambda p, d: True, _m_substitution),
}


def _b_reserialize(p, d, rng, ctx):
    # pretty-printed, reversed key order, re-parsed: semantically identical evidence
    def rev(o):
        if isinstance(o, dict):
            return {k: rev(o[k]) for k in reversed(list(o))}
        if isinstance(o, list):
            return [rev(x) for x in o]
        return o
    return json.loads(json.dumps(rev(p), indent=4, ensure_ascii=True)), d

def _b_unicode_nfd(p, d, rng, ctx):
    p["context"]["entity"] = unicodedata.normalize("NFD", p["context"]["entity"]); return p, d


BENIGN = {"reserialize_reorder": _b_reserialize, "unicode_nfd": _b_unicode_nfd}


def run_tamper(check, store, record_ids: list[bytes], approvers: list[str], seed: int) -> list[dict]:
    """check(record_id, h) -> (matches_on_chain_commitment, record); see loadgen.commitment_checker."""
    rng = np.random.default_rng(seed)
    rows = []
    for k, rid in enumerate(record_ids):
        base_pkg, base_docs = store.get(rid)
        other_pkg, other_docs = store.get(record_ids[(k + 1) % len(record_ids)])
        ctx = {"approvers": approvers, "other_pkg": other_pkg, "other_docs": other_docs}
        # sanity: unmodified evidence must verify
        rows.append(_check(rid, "none", "control", base_pkg, base_docs, check, expect_detect=False))
        for name, (applies, mutate) in MODIFICATIONS.items():
            if not applies(base_pkg, base_docs):
                continue
            p, d = mutate(copy.deepcopy(base_pkg), dict(base_docs), rng, ctx)
            rows.append(_check(rid, name, "malicious", p, d, check, expect_detect=True))
        for name, transform in BENIGN.items():
            p, d = transform(copy.deepcopy(base_pkg), dict(base_docs), rng, ctx)
            rows.append(_check(rid, name, "benign", p, d, check, expect_detect=False))
    return rows


def _check(rid, name, kind, pkg, docs, check, expect_detect):
    t0 = time.perf_counter()
    h = evidence.recompute(pkg, docs)
    match, _ = check(rid, h)
    dt = time.perf_counter() - t0
    detected = not match
    return {"record_id": rid.hex(), "modification": name, "kind": kind, "detected": detected,
            "correct": detected == expect_detect, "verify_s": dt}
