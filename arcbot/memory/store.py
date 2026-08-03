"""SQLite-backed persistence for memory records (stdlib ``sqlite3`` only)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .types import MemoryRecord, MemoryType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    mtype         TEXT NOT NULL,
    content       TEXT NOT NULL,
    keywords      TEXT,
    importance    REAL DEFAULT 0.5,
    pinned        INTEGER DEFAULT 0,
    created_at    REAL,
    last_accessed REAL,
    access_count  INTEGER DEFAULT 0,
    source        TEXT,
    meta          TEXT,
    expires_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_mtype   ON memories(mtype);
CREATE INDEX IF NOT EXISTS idx_pinned  ON memories(pinned);
CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at);
"""

_COLS = (
    "id, mtype, content, keywords, importance, pinned, created_at, "
    "last_accessed, access_count, source, meta, expires_at"
)


class MemoryStore:
    """Thread-safe CRUD over a single SQLite file."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- writes ---------------------------------------------------------------
    def upsert(self, rec: MemoryRecord) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO memories ({_COLS}) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rec.to_row(),
            )
            self._conn.commit()

    def delete(self, mem_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id=?", (mem_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def touch(self, mem_id: str) -> None:
        """Record an access (recency + frequency signal)."""
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_accessed=?, access_count=access_count+1 "
                "WHERE id=?",
                (time.time(), mem_id),
            )
            self._conn.commit()

    def clear(self, mtype: MemoryType | None = None) -> int:
        with self._lock:
            if mtype is None:
                cur = self._conn.execute("DELETE FROM memories")
            else:
                cur = self._conn.execute(
                    "DELETE FROM memories WHERE mtype=?", (mtype.value,)
                )
            self._conn.commit()
            return cur.rowcount

    def purge_expired(self) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            self._conn.commit()
            return cur.rowcount

    # -- reads ----------------------------------------------------------------
    def get(self, mem_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM memories WHERE id=?", (mem_id,)
            ).fetchone()
        return MemoryRecord.from_row(row) if row else None

    def all(
        self,
        mtype: MemoryType | None = None,
        include_expired: bool = False,
        limit: int | None = None,
    ) -> list[MemoryRecord]:
        q = f"SELECT {_COLS} FROM memories"
        params: list = []
        clauses = []
        if mtype is not None:
            clauses.append("mtype=?")
            params.append(mtype.value)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(time.time())
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY pinned DESC, importance DESC, created_at DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [MemoryRecord.from_row(r) for r in rows]

    def count(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mtype, COUNT(*) FROM memories GROUP BY mtype"
            ).fetchall()
        return dict(rows)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
