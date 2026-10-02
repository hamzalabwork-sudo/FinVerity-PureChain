"""Synthetic audit-evidence generator: E_i = {M_i, D_i, A_i, L_i, C_i}.

For every workload transaction T_i a deterministic evidence package is produced:
    M_i  transaction metadata                         (all levels)
    D_i  invoice / purchase-order record + documents  (level >= 2)
    A_i  authorization record                         (level >= 3)
    L_i  double-entry ledger (journal) record         (level >= 4)
    C_i  contextual audit information                 (all levels)

Complexity levels follow the methodology:
    1 metadata only | 2 + invoice | 3 + authorization | 4 complete package

Supporting documents are represented by their SHA-256 digests inside the package; the
document bytes live next to the package in the off-chain store. Document bytes are
incompressible pseudo-random content with a PDF header, which reproduces the size and
hashing cost of scanned invoices/receipts. Everything is seeded by (seed, tx_id), so the
same workload always yields byte-identical evidence.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from .canonical import canonicalize, sha256

SCHEMA_VERSION = "finverity.evidence/1.0"
GENERATOR_VERSION = "finverity-evidence-gen/1.0"

_VENDOR_A = ["Apex", "Blue River", "Cedar", "Delta", "Evergreen", "Falcon", "Granite", "Harbor",
             "Ironwood", "Juniper", "Keystone", "Lumen", "Meridian", "Northwind", "Orion", "Pioneer"]
_VENDOR_B = ["Supplies", "Logistics", "Consulting", "Trading", "Services", "Holdings",
             "Technologies", "Distribution", "Partners", "Industries"]
_VENDOR_C = ["Ltd", "LLC", "Co.", "Inc.", "GmbH", "PLC"]
_ITEMS = ["Professional services", "Office supplies", "Freight charges", "Software licence",
          "Maintenance fee", "Raw materials", "Consulting hours", "Equipment rental",
          "Telecom services", "Utilities", "Marketing services", "Training"]

# (debit GL, credit GL) by transaction category
_GL = {
    "PAYMENT": (("5000", "Operating expenses"), ("1010", "Cash at bank")),
    "TRANSFER": (("2100", "Accounts payable"), ("1010", "Cash at bank")),
    "CASH_OUT": (("1020", "Petty cash"), ("1010", "Cash at bank")),
    "CASH_IN": (("1010", "Cash at bank"), ("4000", "Revenue")),
    "DEBIT": (("5100", "Bank charges"), ("1010", "Cash at bank")),
    "DEPOSIT": (("1010", "Cash at bank"), ("2300", "Customer deposits")),
    "WIRE": (("2100", "Accounts payable"), ("1010", "Cash at bank")),
    "ACH": (("2100", "Accounts payable"), ("1010", "Cash at bank")),
    "CHEQUE": (("2100", "Accounts payable"), ("1010", "Cash at bank")),
    "CREDIT_CARD": (("5000", "Operating expenses"), ("2200", "Credit card payable")),
    "DEBIT_CARD": (("5000", "Operating expenses"), ("1010", "Cash at bank")),
    "WITHDRAWAL": (("1020", "Petty cash"), ("1010", "Cash at bank")),
    "CASH": (("1020", "Petty cash"), ("1010", "Cash at bank")),
    "CASH_DEPOSIT": (("1010", "Cash at bank"), ("1020", "Petty cash")),
    "CROSS_BORDER": (("2100", "Accounts payable"), ("1010", "Cash at bank")),
    "REINVESTMENT": (("1500", "Investments"), ("1010", "Cash at bank")),
    "BITCOIN": (("1600", "Digital assets"), ("1010", "Cash at bank")),
}
_DEFAULT_GL = (("1990", "Suspense account"), ("1010", "Cash at bank"))

# approval tiers by amount (policy P-FIN-07, synthetic)
_TIERS = [(Decimal("1000"), 1, "Team lead"), (Decimal("10000"), 2, "Finance manager"),
          (Decimal("Infinity"), 3, "CFO")]


@dataclass
class EvidenceBundle:
    tx_id: str
    level: int
    package: dict
    documents: dict[str, bytes]   # doc_id -> bytes
    approver_index: int
    canonical: bytes              # Canon(E_i)
    evidence_hash: bytes          # H_i
    meta_hash: bytes              # commitment to authorization/validation metadata

    @property
    def size_bytes(self) -> int:
        return len(self.canonical) + sum(len(b) for b in self.documents.values())


def _rng(seed: int, tx_id: str) -> np.random.Generator:
    h = hashlib.sha256(f"{seed}|{tx_id}".encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "big"))


def _cents(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _vendor(beneficiary: str) -> dict:
    h = hashlib.sha256(beneficiary.encode()).digest()
    name = f"{_VENDOR_A[h[0] % 16]} {_VENDOR_B[h[1] % 10]} {_VENDOR_C[h[2] % 6]}"
    return {"vendor_id": f"V-{int.from_bytes(h[3:6], 'big'):07d}", "name": name,
            "account": beneficiary, "tax_id": f"TX{int.from_bytes(h[6:10], 'big') % 10**9:09d}"}


def _document(rng: np.random.Generator, doc_id: str, kind: str, size: int) -> bytes:
    header = f"%PDF-1.7\n% FinVerity synthetic {kind} {doc_id}\n".encode()
    if size <= len(header):
        return header[:max(size, 1)]
    return header + rng.bytes(size - len(header))


def _split_amount(rng: np.random.Generator, total: Decimal, k: int) -> list[Decimal]:
    cents = int(total * 100)
    if k == 1 or cents < k:
        return [total]
    # sample k-1 distinct cut points in [1, cents) without materialising the range (amounts can be ~1e12 cents)
    cuts = np.sort(rng.choice(cents - 1, size=k - 1, replace=False) + 1)
    parts = np.diff(np.concatenate(([0], cuts, [cents])))
    return [Decimal(int(p)) / 100 for p in parts]


def generate(tx: dict, level: int, doc_bytes: int, seed: int, approver_addresses: list[str],
             run_id: str = "", approver_index: int | None = None) -> EvidenceBundle:
    if level not in (1, 2, 3, 4):
        raise ValueError("level must be 1..4")
    rng = _rng(seed, tx["tx_id"])
    ts = datetime.strptime(tx["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    amount = Decimal(tx["amount"])
    year = ts.year
    seq = int(tx["seq"])

    tier_level, tier_role = next((lvl, role) for limit, lvl, role in _TIERS if amount < limit)
    drawn = int(rng.integers(0, len(approver_addresses)))
    approver_index = drawn if approver_index is None else approver_index
    approver = approver_addresses[approver_index]

    package: dict = {
        "schema": SCHEMA_VERSION,
        "record": {"tx_id": tx["tx_id"], "source": tx["source"], "seq": seq, "run_id": run_id},
        "level": level,
        "metadata": {
            "timestamp": tx["timestamp"], "type": tx["tx_type"], "amount": tx["amount"],
            "currency": tx["currency"], "originator": tx["originator"],
            "beneficiary": tx["beneficiary"], "channel": tx["channel"],
            "balances": {"orig_before": tx["orig_bal_before"], "orig_after": tx["orig_bal_after"],
                         "dest_before": tx["dest_bal_before"], "dest_after": tx["dest_bal_after"]},
            "locations": {"orig": tx["orig_location"], "dest": tx["dest_location"]},
        },
        "attestation": {"approver": approver},
        "context": {
            "generator": GENERATOR_VERSION, "seed": seed, "dataset": tx["source"],
            "dataset_row": seq, "entity": "FinVerity Démo Société S.A.", "fiscal_period": f"{year}-{ts.month:02d}",
        },
    }
    documents: dict[str, bytes] = {}

    if level >= 2:
        n_items = int(rng.integers(1, 6))
        gross = amount
        net = (gross / Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tax = gross - net
        lines = [{"line": i + 1, "description": _ITEMS[int(rng.integers(0, len(_ITEMS)))],
                  "amount": _cents(p)} for i, p in enumerate(_split_amount(rng, net, n_items))]
        issue = ts - timedelta(days=int(rng.integers(0, 31)), minutes=int(rng.integers(0, 1440)))
        package["invoice"] = {
            "invoice_no": f"INV-{year}-{seq % 10**7:07d}",
            "purchase_order": f"PO-{year}-{int(rng.integers(0, 10**6)):06d}",
            "vendor": _vendor(tx["beneficiary"]),
            "issue_date": issue.strftime("%Y-%m-%d"),
            "due_date": (issue + timedelta(days=30)).strftime("%Y-%m-%d"),
            "lines": lines, "net": _cents(net), "tax_rate": "0.10", "tax": _cents(tax),
            "total": _cents(gross), "currency": tx["currency"],
        }
        kinds = ["invoice_scan", "purchase_order", "payment_receipt"][: {2: 1, 3: 2, 4: 3}[level]]
        if doc_bytes > 0:
            share = doc_bytes // len(kinds)
            refs = []
            for j, kind in enumerate(kinds):
                size = share + (doc_bytes - share * len(kinds) if j == 0 else 0)
                doc_id = f"DOC-{tx['tx_id']}-{j + 1}"
                blob = _document(rng, doc_id, kind, size)
                digest = sha256(blob).hex()
                documents[doc_id] = blob
                refs.append({"doc_id": doc_id, "type": kind, "media_type": "application/pdf",
                             "size": len(blob), "sha256": digest})
            package["documents"] = refs

    if level >= 3:
        approved_at = ts - timedelta(minutes=int(rng.integers(1, 240)), seconds=int(rng.integers(0, 60)))
        package["authorization"] = {
            "approval_id": f"APR-{year}-{int(rng.integers(0, 10**7)):07d}",
            "approver": approver, "approver_role": tier_role, "approval_tier": tier_level,
            "approved_at": approved_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "policy": "P-FIN-07 delegation of authority", "segregation_of_duties": True,
            "dual_control": tier_level == 3,
        }

    if level >= 4:
        (dr_code, dr_name), (cr_code, cr_name) = _GL.get(tx["tx_type"], _DEFAULT_GL)
        package["ledger"] = {
            "journal_id": f"JE-{year}-{seq % 10**8:08d}",
            "gl_reference": f"GL-{year}-{int(rng.integers(0, 10**6)):06d}",
            "posting_date": ts.strftime("%Y-%m-%d"),
            "entries": [
                {"account": dr_code, "name": dr_name, "debit": tx["amount"], "credit": "0.00"},
                {"account": cr_code, "name": cr_name, "debit": "0.00", "credit": tx["amount"]},
            ],
            "balanced": True,
        }

    canonical = canonicalize(package)
    meta = {"authorization": package.get("authorization"), "attestation": package["attestation"],
            "validation": {"level": level, "schema": SCHEMA_VERSION, "hash_alg": "SHA-256",
                           "canonicalization": "RFC8785-profile"}}
    return EvidenceBundle(
        tx_id=tx["tx_id"], level=level, package=package, documents=documents,
        approver_index=approver_index, canonical=canonical,
        evidence_hash=sha256(canonical), meta_hash=sha256(canonicalize(meta)),
    )


def recompute(package: dict, documents: dict[str, bytes]) -> bytes:
    """Verifier side: H_i' = H(E_i').

    Every stored document is re-hashed and its fresh digest substituted into the package
    before the canonical hash is recomputed, so any change to a document, to a package
    field, or a missing document yields H_i' != H_i.
    """
    if "documents" in package:
        package = dict(package)
        package["documents"] = [
            {**ref, "sha256": sha256(documents[ref["doc_id"]]).hex() if ref["doc_id"] in documents else "missing"}
            for ref in package["documents"]
        ]
    return sha256(canonicalize(package))
