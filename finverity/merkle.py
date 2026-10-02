"""Merkle commitments for batched evidence recording (mirrors FinVerityRegistry.verifyInclusion).

leaf = keccak256(recordId || evidenceHash); internal nodes use sorted-pair keccak256, so a
proof is just the list of sibling hashes. An odd node at any level is promoted unchanged.
"""
from __future__ import annotations

from eth_utils import keccak


def leaf(record_id: bytes, evidence_hash: bytes) -> bytes:
    return keccak(record_id + evidence_hash)


def _pair(a: bytes, b: bytes) -> bytes:
    return keccak(a + b) if a < b else keccak(b + a)


def build(leaves: list[bytes]) -> tuple[bytes, list[list[bytes]]]:
    """Return (root, proofs) where proofs[i] authenticates leaves[i]."""
    if not leaves:
        raise ValueError("empty batch")
    proofs: list[list[bytes]] = [[] for _ in leaves]
    level = list(leaves)
    pos = list(range(len(leaves)))          # index of each original leaf at the current level
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_pair(level[i], level[i + 1]))
            else:
                nxt.append(level[i])
        for j, p in enumerate(pos):
            sib = p ^ 1
            if sib < len(level):
                proofs[j].append(level[sib])
            pos[j] = p // 2
        level = nxt
    return level[0], proofs


def verify(leaf_hash: bytes, proof: list[bytes], root: bytes) -> bool:
    h = leaf_hash
    for p in proof:
        h = _pair(h, p)
    return h == root
