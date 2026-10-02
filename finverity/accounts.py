"""Deterministic role accounts (submitters, approvers, auditors).

Keys are derived from the admin key so every run is reproducible without storing
additional secrets: k_{role,i} = keccak256(admin_key || role || i).
PureChain charges no gas, so these accounts never need funding.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak

from .config import admin_key


@lru_cache(maxsize=None)
def derive(role: str, index: int) -> LocalAccount:
    seed = bytes.fromhex(admin_key().removeprefix("0x")) + role.encode() + index.to_bytes(4, "big")
    return Account.from_key(keccak(seed))


def admin() -> LocalAccount:
    return Account.from_key(admin_key())


@dataclass(frozen=True)
class RoleSet:
    submitters: list[LocalAccount]
    approvers: list[LocalAccount]


def roles(n_submitters: int = 16, n_approvers: int = 24) -> RoleSet:
    return RoleSet(
        submitters=[derive("submitter", i) for i in range(n_submitters)],
        approvers=[derive("approver", i) for i in range(n_approvers)],
    )
