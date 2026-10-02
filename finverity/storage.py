"""Off-chain evidence repository.

Evidence packages and supporting documents never go on-chain; they are kept in an
off-chain store that stands in for an organisation's document-management system.
SQLite (WAL, synchronous=FULL) gives durable, single-file storage and avoids creating
hundreds of thousands of small files.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .config import EVIDENCE_STORE
from .evidence import EvidenceBundle


class EvidenceStore:
    def __init__(self, run_id: str, root: Path = EVIDENCE_STORE):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{run_id}.sqlite"
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        con = self._con()
        con.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS packages(
                record_id BLOB PRIMARY KEY, tx_id TEXT NOT NULL, level INTEGER NOT NULL,
                package BLOB NOT NULL, evidence_hash BLOB NOT NULL, approver_index INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS proofs(
                record_id BLOB PRIMARY KEY, batch_id BLOB NOT NULL, proof BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(
                record_id BLOB NOT NULL, doc_id TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY(record_id, doc_id));
        """)

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
            con.execute("PRAGMA synchronous=FULL")
            self._local.con = con
            with self._lock:
                self._all.append(con)
        return con

    def put(self, record_id: bytes, b: EvidenceBundle) -> None:
        con = self._con()
        with con:
            con.execute("INSERT OR REPLACE INTO packages VALUES(?,?,?,?,?,?)",
                        (record_id, b.tx_id, b.level, b.canonical, b.evidence_hash, b.approver_index))
            con.executemany("INSERT OR REPLACE INTO documents VALUES(?,?,?)",
                            [(record_id, d, c) for d, c in b.documents.items()])

    def put_proofs(self, rows: list[tuple[bytes, bytes, list[bytes]]]) -> None:
        con = self._con()
        with con:
            con.executemany("INSERT OR REPLACE INTO proofs VALUES(?,?,?)",
                            [(rid, bid, b"".join(pr)) for rid, bid, pr in rows])

    def get_proof(self, record_id: bytes) -> tuple[bytes, list[bytes]] | None:
        row = self._con().execute("SELECT batch_id, proof FROM proofs WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            return None
        return row[0], [row[1][i:i + 32] for i in range(0, len(row[1]), 32)]

    def get(self, record_id: bytes) -> tuple[dict, dict[str, bytes]]:
        con = self._con()
        row = con.execute("SELECT package FROM packages WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id.hex())
        docs = dict(con.execute("SELECT doc_id, content FROM documents WHERE record_id=?", (record_id,)))
        return json.loads(row[0]), docs

    def record_ids(self) -> list[bytes]:
        return [r[0] for r in self._con().execute("SELECT record_id FROM packages ORDER BY rowid")]

    def size_bytes(self) -> int:
        con = self._con()
        pk = con.execute("SELECT COALESCE(SUM(LENGTH(package)),0) FROM packages").fetchone()[0]
        dc = con.execute("SELECT COALESCE(SUM(LENGTH(content)),0) FROM documents").fetchone()[0]
        return int(pk + dc)

    def close(self) -> None:
        with self._lock:
            for con in self._all:
                con.close()
            self._all.clear()
        self._local = threading.local()

    def delete(self) -> None:
        self.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
