"""Backend interface shared by FinVerity (PureChain), Besu QBFT and the database baseline."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from eth_account.messages import encode_defunct
from eth_abi import encode as abi_encode
from eth_utils import keccak


@dataclass
class Item:
    """One evidence-recording request R_i = {ID_i, H_i, t_i, S_i, A_i, V_i}."""
    idx: int
    tx_id: str
    record_id: bytes
    evidence_hash: bytes
    meta_hash: bytes
    level: int
    approver_index: int
    submitter_index: int
    evidence_bytes: int
    approval: bytes = b""
    payload: object = None          # backend-specific (signed raw tx, SQL row, ...)
    payload_bytes: int = 0
    # timings (perf_counter seconds)
    t_sched: float = 0.0
    t_send: float = 0.0
    t_ack: float = 0.0
    t_commit: float = 0.0
    tx_hash: str = ""
    block: int = -1
    block_hash: str = ""
    t_first_seen: float = 0.0               # first sighting in any block version (optimistic)
    orphaned_sightings: int = 0             # sightings in blocks later replaced by a fork
    conf: dict = field(default_factory=dict)  # k -> time block+k was first observed
    members: list = field(default_factory=list)  # batch unit: the records it commits
    t_final: float = 0.0                          # finality under the backend's finality rule
    gas_used: int = 0
    status: str = "pending"          # pending | committed | reverted | failed | timeout
    error: str = ""
    prep: dict = field(default_factory=dict)


def approval_digest(chain_id: int, registry: str, record_id: bytes, evidence_hash: bytes,
                    meta_hash: bytes, level: int) -> bytes:
    """Inner struct hash signed by the approver; must mirror FinVerityRegistry.approvalDigest."""
    return keccak(abi_encode(["uint256", "address", "bytes32", "bytes32", "bytes32", "uint8"],
                             [chain_id, registry, record_id, evidence_hash, meta_hash, level]))


def sign_approval(approver, chain_id: int, registry: str, item: Item) -> bytes:
    inner = approval_digest(chain_id, registry, item.record_id, item.evidence_hash, item.meta_hash, item.level)
    return bytes(approver.sign_message(encode_defunct(primitive=inner)).signature)


class Backend(ABC):
    name: str = "backend"
    kind: str = "blockchain"
    containers: list[str] = []      # docker containers to meter (local backends)
    n_validators: int = 1

    @abstractmethod
    def setup(self, submitters, approvers) -> dict: ...

    @abstractmethod
    def prepare(self, item: Item, submitter, approver) -> None: ...

    def start(self, workers: int = 1) -> None: ...

    @abstractmethod
    def send(self, item: Item, worker: int) -> None:
        """Submit one item; must set t_send/t_ack (and t_commit if synchronous)."""

    def wait(self, items: list[Item], timeout_s: float) -> None: ...

    def stop(self) -> None: ...

    def fetch_batch(self, batch_id: bytes) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def fetch_record(self, record_id: bytes) -> dict | None:
        """Return {'evidence_hash','approver','submitter','timestamp','block'} or None."""

    def info(self) -> dict:
        return {"backend": self.name, "kind": self.kind, "validators": self.n_validators}

    def block_log(self) -> list[dict]:
        return []

    def extra_metrics(self, items: list[Item]) -> dict:
        return {}
