"""Thread-safe SQLite access and schema management."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from winfirst.core.migrations import SCHEMA_VERSION, apply_migrations
from winfirst.core.paths import database_path


class DatabaseError(RuntimeError):
    pass


class Database:
    """A single connection guarded by a re-entrant lock.

    SQLite is used for modest volumes of local data; serialising access keeps
    GUI and worker threads from interleaving partial writes.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else database_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                str(self.path), timeout=20, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not open the WinFirst database: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=20000")
            self.schema_version = apply_migrations(self._conn)
        except Exception:
            self._conn.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor.lastrowid or 0
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise DatabaseError(f"Database write failed: {exc}") from exc

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            try:
                self._conn.executemany(sql, rows)
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise DatabaseError(f"Database write failed: {exc}") from exc

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.Error as exc:
                raise DatabaseError(f"Database read failed: {exc}") from exc

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def integrity_check(self) -> str:
        row = self.query_one("PRAGMA integrity_check")
        return str(next(iter(row.values()))) if row else "unknown"

    def stats(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "supported_schema": SCHEMA_VERSION,
            "operations": self.scalar("SELECT COUNT(*) FROM operations", default=0),
            "profiles": self.scalar("SELECT COUNT(*) FROM profiles", default=0),
            "rollback_entries": self.scalar("SELECT COUNT(*) FROM rollback_entries", default=0),
            "reports": self.scalar("SELECT COUNT(*) FROM reports", default=0),
        }
