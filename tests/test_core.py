"""Offline checks of canonicalization, evidence generation, signatures and tamper detection."""
import json

import pytest

from finverity import accounts, evidence, tamper
from finverity.backends.base import Item, approval_digest, sign_approval
from finverity.canonical import CanonicalizationError, canonicalize, commit

TX = {"tx_id": "MOM-00000042", "source": "momtsim", "seq": 42, "timestamp": "2024-01-01T05:12:33Z",
      "tx_type": "PAYMENT", "amount": "4250.00", "currency": "XXX", "originator": "4018807983755588",
      "beneficiary": "30-0000345", "orig_bal_before": "5000.00", "orig_bal_after": "750.00",
      "dest_bal_before": "0.00", "dest_bal_after": "0.00", "channel": "MOBILE_MONEY",
      "orig_location": "", "dest_location": ""}
APPROVERS = [a.address for a in accounts.roles(2, 4).approvers]


def test_canonical_is_order_and_whitespace_invariant():
    a = {"b": 1, "a": {"y": "é", "x": [1, 2]}}
    b = json.loads(json.dumps({"a": {"x": [1, 2], "y": "é"}, "b": 1}, indent=3))
    assert canonicalize(a) == canonicalize(b) == '{"a":{"x":[1,2],"y":"é"},"b":1}'.encode()


def test_floats_rejected():
    with pytest.raises(CanonicalizationError):
        canonicalize({"amount": 1.5})


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_generation_is_deterministic(level):
    b1 = evidence.generate(TX, level, 50_000, seed=7, approver_addresses=APPROVERS)
    b2 = evidence.generate(TX, level, 50_000, seed=7, approver_addresses=APPROVERS)
    assert b1.evidence_hash == b2.evidence_hash and b1.documents == b2.documents
    assert evidence.recompute(json.loads(b1.canonical), b1.documents) == b1.evidence_hash
    if level >= 2:
        assert sum(len(x) for x in b1.documents.values()) == 50_000


def test_ledger_balanced_and_invoice_totals():
    b = evidence.generate(TX, 4, 0, seed=1, approver_addresses=APPROVERS)
    inv = b.package["invoice"]
    from decimal import Decimal
    assert sum(Decimal(l["amount"]) for l in inv["lines"]) == Decimal(inv["net"])
    assert Decimal(inv["net"]) + Decimal(inv["tax"]) == Decimal(inv["total"]) == Decimal(TX["amount"])
    e = b.package["ledger"]["entries"]
    assert e[0]["debit"] == e[1]["credit"] == TX["amount"]


def test_approval_signature_recovers():
    from eth_account import Account
    from eth_account.messages import encode_defunct
    appr = accounts.roles(2, 4).approvers[1]
    it = Item(0, "x", b"\x01" * 32, b"\x02" * 32, b"\x03" * 32, 4, 1, 0, 0)
    sig = sign_approval(appr, 900520900520, "0x" + "11" * 20, it)
    inner = approval_digest(900520900520, "0x" + "11" * 20, it.record_id, it.evidence_hash, it.meta_hash, 4)
    assert Account.recover_message(encode_defunct(primitive=inner), signature=sig) == appr.address


class _FakeBackend:
    def __init__(self, hashes):
        self.hashes = hashes

    def fetch_record(self, rid):
        return {"evidence_hash": self.hashes[rid]}


class _FakeStore:
    def __init__(self, bundles):
        self.b = bundles

    def get(self, rid):
        b = self.b[rid]
        return json.loads(b.canonical), dict(b.documents)


def test_tamper_detection_complete_and_no_false_positives():
    bundles = {}
    for i in range(5):
        tx = {**TX, "tx_id": f"MOM-{i:08d}", "seq": i, "amount": f"{100 + i}.25"}
        bundles[bytes([i]) * 32] = evidence.generate(tx, 4, 20_000, seed=3, approver_addresses=APPROVERS)
    hashes = {k: v.evidence_hash for k, v in bundles.items()}
    rows = tamper.run_tamper(lambda rid, h: (hashes[rid] == h, None), _FakeStore(bundles), list(bundles),
                             APPROVERS, seed=0)
    assert all(r["correct"] for r in rows), [r for r in rows if not r["correct"]]
    kinds = {r["modification"] for r in rows if r["kind"] == "malicious"}
    assert kinds == set(tamper.MODIFICATIONS)


def test_contract_compiles():
    from finverity.contract import compile_registry
    art = compile_registry(force=True)
    assert any(f.get("name") == "recordEvidence" for f in art["abi"])
    assert art["deployed_bytecode_size"] < 24_576


@pytest.mark.parametrize("n", [1, 2, 3, 7, 64, 100])
def test_merkle_proofs(n):
    from finverity import merkle
    leaves = [merkle.leaf(bytes([i % 256]) * 32, bytes([(i * 7) % 256]) * 32) for i in range(n)]
    root, proofs = merkle.build(leaves)
    assert all(merkle.verify(l, p, root) for l, p in zip(leaves, proofs))
    assert not merkle.verify(merkle.leaf(b"\xff" * 32, b"\xee" * 32), proofs[0], root) or n == 0


def test_form_batches_policy():
    import numpy as np
    from finverity.loadgen import form_batches
    sched = np.array([0.0, 0.1, 0.2, 0.3, 2.0, 2.05, 5.0])
    groups = form_batches(sched, k=3, wait_s=1.0)
    assert [g for g, _ in groups] == [[0, 1, 2], [3], [4, 5], [6]]
    assert groups[0][1] == 0.2 and groups[1][1] == 1.3      # size-close, then wait-close
    assert sum(len(g) for g, _ in groups) == len(sched)


def test_huge_amounts_split_without_memory_blowup():
    tx = {**TX, "amount": "13524530712.55"}          # IBM AML P99-scale amount
    b = evidence.generate(tx, 4, 0, seed=1, approver_addresses=APPROVERS)
    from decimal import Decimal
    inv = b.package["invoice"]
    assert sum(Decimal(l["amount"]) for l in inv["lines"]) == Decimal(inv["net"])
