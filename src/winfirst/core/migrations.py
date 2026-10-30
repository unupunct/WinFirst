"""SQLite schema migrations.

Migrations are applied in order using ``PRAGMA user_version``.  Each migration
runs inside a transaction; a failure rolls back and aborts startup with a clear
error instead of leaving a half-migrated database.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_profile_versions_profile ON profile_versions(profile_id, version);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    risk TEXT NOT NULL DEFAULT 'informational',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    windows_version TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    requires_admin INTEGER NOT NULL DEFAULT 0,
    elevated INTEGER NOT NULL DEFAULT 0,
    restart_required INTEGER NOT NULL DEFAULT 0,
    rollback_available INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    requested_state TEXT NOT NULL DEFAULT '',
    previous_state TEXT NOT NULL DEFAULT '',
    actual_state TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_operations_started ON operations(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_operations_category ON operations(category);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);

CREATE TABLE IF NOT EXISTS operation_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_row_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    FOREIGN KEY(operation_row_id) REFERENCES operations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_operation_details_row ON operation_details(operation_row_id);

CREATE TABLE IF NOT EXISTS rollback_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_row_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available',
    created_at TEXT NOT NULL,
    restored_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(operation_row_id) REFERENCES operations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rollback_status ON rollback_entries(status);

CREATE TABLE IF NOT EXISTS software_catalog_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checklist_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_builtin INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployment_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_name TEXT NOT NULL,
    profile_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS deployment_task_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    task_key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, task_key),
    FOREIGN KEY(session_id) REFERENCES deployment_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS repository_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'other',
    name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    signature_status TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS drift_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    differences INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL DEFAULT '{}'
);
"""

MIGRATIONS: list[tuple[int, str, str]] = [(1, "initial schema", MIGRATION_1)]


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"The database schema version ({version}) is newer than this build supports "
            f"({SCHEMA_VERSION}). Update WinFirst or use a different data directory."
        )
    for target, name, script in MIGRATIONS:
        if target <= version:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_statements(script):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise RuntimeError(f"Database migration {target} ({name}) failed: {exc}") from exc
        version = target
    return version


def _split_statements(script: str) -> list[str]:
    """Split a migration on semicolons outside of string literals."""
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in script:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            buffer.append(char)
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            continue
        buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements
