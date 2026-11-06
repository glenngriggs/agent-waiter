"""SQLite storage: the single source of durable truth for the platform.

One connection guarded by a re-entrant lock. Calls are milliseconds long,
so async code calls these methods directly instead of hopping threads.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    setup_state TEXT NOT NULL,
    setup_detail TEXT,
    token_version INTEGER NOT NULL DEFAULT 1,
    chat_thread_id TEXT,
    conversation_no INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    job_run_id TEXT,
    conversation_no INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    codex_thread_id TEXT,
    codex_turn_id TEXT,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    final_response TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_kind TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_by TEXT NOT NULL,
    source_turn_id TEXT,
    status TEXT NOT NULL,
    next_run_at TEXT,
    last_run_id TEXT,
    created_at TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL,
    late INTEGER NOT NULL DEFAULT 0,
    trigger TEXT NOT NULL,
    turn_id TEXT,
    summary TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    turn_id TEXT,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_turns_agent_status ON turns(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_agent_status ON jobs(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_status_next ON jobs(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """One canonical text form so timestamps sort correctly as strings."""
    if dt.tzinfo is None:
        raise ValueError("iso() needs a timezone-aware datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._depth = 0  # transaction nesting depth, guarded by _lock
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")

    def execute(self, sql: str, params: tuple | list | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | list | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple | list | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """Hold the lock for the whole block; nested use joins the outer one."""
        with self._lock:
            if self._depth > 0:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._conn.execute("COMMIT")

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA user_version = 1")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
