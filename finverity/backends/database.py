"""Baseline 1: centralized database (PostgreSQL, or SQLite as a zero-setup fallback).

Performs the same logical operation as the smart contract - submitter authorization,
approver signature recovery, duplicate check, durable insert - but with a single
trusted operator and no replication or consensus.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from eth_account import Account
from eth_account.messages import encode_defunct

from ..config import EVIDENCE_STORE, POSTGRES_DSN
from .base import Backend, Item, approval_digest, sign_approval

_ZERO = "0x0000000000000000000000000000000000000000"

DDL = """
CREATE TABLE IF NOT EXISTS evidence_records(
    record_id BYTEA PRIMARY KEY, evidence_hash BYTEA NOT NULL, meta_hash BYTEA NOT NULL,
    submitter TEXT NOT NULL, approver TEXT NOT NULL, level SMALLINT NOT NULL,
    approval BYTEA NOT NULL, recorded_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_batches(
    batch_id BYTEA PRIMARY KEY, merkle_root BYTEA NOT NULL, meta_hash BYTEA NOT NULL,
    submitter TEXT NOT NULL, approver TEXT NOT NULL, level SMALLINT NOT NULL, count INTEGER NOT NULL,
    approval BYTEA NOT NULL, recorded_at DOUBLE PRECISION NOT NULL)
"""


class DatabaseBackend(Backend):
    kind = "database"

    def __init__(self, engine: str = "postgres", containers: list[str] | None = None):
        self.engine = engine
        self.name = f"db_{engine}"
        self.containers = containers or (["finverity-postgres"] if engine == "postgres" else [])
        self._local = threading.local()
        self.ph = "%s" if engine == "postgres" else "?"

    def start(self, workers: int = 1) -> None:
        # one persistent connection per submission worker, opened before measurement
        self._pool = {w: self._open() for w in range(workers)}

    def _con(self, worker: int | None = None):
        if worker is not None and worker in getattr(self, "_pool", {}):
            return self._pool[worker]
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = self._open()
        return con

    def _open(self):
        if self.engine == "postgres":
            import psycopg
            return psycopg.connect(POSTGRES_DSN, autocommit=False)
        EVIDENCE_STORE.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(EVIDENCE_STORE / "baseline_db.sqlite", timeout=60, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def setup(self, submitters, approvers) -> dict:
        con = self._con()
        ddl = DDL if self.engine == "postgres" else DDL.replace("BYTEA", "BLOB").replace("DOUBLE PRECISION", "REAL")
        for stmt in ddl.split(";"):
            if stmt.strip():
                con.execute(stmt)
        con.commit()
        self.submitters = {a.address for a in submitters}
        self.approvers = {a.address for a in approvers}
        return {"engine": self.engine}

    def prepare(self, item: Item, submitter, approver) -> None:
        t0 = time.perf_counter()
        item.approval = sign_approval(approver, 0, _ZERO, item)
        item.prep["approval_sign_s"] = time.perf_counter() - t0
        item.prep["tx_build_sign_s"] = 0.0
        item.payload = submitter.address
        item.payload_bytes = 32 * 3 + 1 + 65 + 20

    def send(self, item: Item, worker: int) -> None:
        item.t_send = time.perf_counter()
        try:
            if item.payload not in self.submitters:
                raise PermissionError("not a submitter")
            inner = approval_digest(0, _ZERO, item.record_id, item.evidence_hash, item.meta_hash, item.level)
            approver = Account.recover_message(encode_defunct(primitive=inner), signature=item.approval)
            if approver not in self.approvers:
                raise PermissionError("invalid approval")
            con = self._con(worker)
            ph = self.ph
            now = time.time()
            if item.members:   # group commit of the whole batch in one durable transaction
                con.execute(f"INSERT INTO evidence_batches VALUES({','.join([ph] * 9)})",
                            (item.record_id, item.evidence_hash, item.meta_hash, item.payload, approver,
                             item.level, len(item.members), item.approval, now))
                cur = con.cursor()
                cur.executemany(f"INSERT INTO evidence_records VALUES({','.join([ph] * 8)})",
                                [(m.record_id, m.evidence_hash, m.meta_hash, item.payload, approver, m.level,
                                  b"", now) for m in item.members])
            else:
                con.execute(f"INSERT INTO evidence_records VALUES({','.join([ph] * 8)})",
                            (item.record_id, item.evidence_hash, item.meta_hash, item.payload, approver,
                             item.level, item.approval, now))
            con.commit()   # durable commit (fsync) = record is final
            item.t_ack = item.t_commit = time.perf_counter()
            item.status = "committed"
            item.block = 0
        except Exception as e:
            item.t_ack = time.perf_counter()
            item.status, item.error = "failed", f"{type(e).__name__}: {e}"[:200]
            try:
                self._con(worker).rollback()
            except Exception:
                pass

    def fetch_batch(self, batch_id: bytes) -> dict | None:
        cur = self._con().execute(
            f"SELECT merkle_root, meta_hash, submitter, approver, recorded_at, level, count "
            f"FROM evidence_batches WHERE batch_id={self.ph}", (batch_id,))
        row = cur.fetchone()
        self._con().commit()
        if row is None:
            return None
        return {"merkle_root": bytes(row[0]), "meta_hash": bytes(row[1]), "submitter": row[2], "approver": row[3],
                "timestamp": row[4], "block": 0, "level": row[5], "count": row[6]}

    def fetch_record(self, record_id: bytes) -> dict | None:
        cur = self._con().execute(
            f"SELECT evidence_hash, meta_hash, submitter, approver, recorded_at, level "
            f"FROM evidence_records WHERE record_id={self.ph}", (record_id,))
        row = cur.fetchone()
        self._con().commit()
        if row is None:
            return None
        return {"evidence_hash": bytes(row[0]), "meta_hash": bytes(row[1]), "submitter": row[2],
                "approver": row[3], "timestamp": row[4], "block": 0, "level": row[5]}

    def info(self) -> dict:
        d = super().info()
        d["engine"] = self.engine
        return d
