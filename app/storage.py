"""SQLite-backed durable storage.

All mutable state lives in SQLite (WAL mode for file-backed databases) so
that multiple threads *and* multiple processes share one source of truth.

Write transactions are started with ``BEGIN IMMEDIATE`` which takes a
database-level reserved lock immediately, serialising writers (and hence
quota decisions) across processes.  Readers run in autocommit mode.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    total_quota INTEGER NOT NULL,
    per_user_quota INTEGER NOT NULL,
    issued_count INTEGER NOT NULL DEFAULT 0,
    rule_version INTEGER,
    start_at TEXT,
    end_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, version)
);

CREATE TABLE IF NOT EXISTS selection_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    rule_version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    selection_hash TEXT NOT NULL,
    matched_count INTEGER NOT NULL,
    user_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, rule_version)
);

CREATE TABLE IF NOT EXISTS vouchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    rule_version INTEGER,
    issued_at TEXT NOT NULL,
    redeemed_at TEXT,
    redeemed_by TEXT,
    redeem_request_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_vouchers_campaign_user
    ON vouchers(campaign_id, user_id);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id TEXT NOT NULL,
    referred_id TEXT NOT NULL,
    campaign_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    reject_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_referral_edge
    ON referrals(referrer_id, referred_id, campaign_id)
    WHERE status = 'valid';

CREATE TABLE IF NOT EXISTS conversion_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    campaign_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_id INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    attributed INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
-- a valid referral can be attributed at most once
CREATE UNIQUE INDEX IF NOT EXISTS ux_attribution_referral_once
    ON attributions(referral_id) WHERE attributed = 1;

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    next_offset INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES batch_jobs(id),
    idx INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    voucher_id INTEGER,
    error TEXT,
    UNIQUE(job_id, idx)
);
"""


class Storage:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._local = threading.local()
        self._mem_conn: sqlite3.Connection | None = None
        self.init_schema()

    # ------------------------------------------------------------------ conn
    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # manual transaction control
            check_same_thread=False,
            timeout=30,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={30_000}")
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._mem_conn is None:
                self._mem_conn = self._new_connection()
            return self._mem_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        with self._lock:
            self._conn().executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised ``BEGIN IMMEDIATE`` transaction.

        The lock makes in-memory multi-threaded use safe; ``BEGIN IMMEDIATE``
        additionally serialises writers across processes on a file database.
        Nested usage shares the outer transaction (SQLite SAVEPOINT-free
        flat nesting: only the outermost context commits).
        """
        outer = not getattr(self._local, "in_tx", False)
        with self._lock:
            conn = self._conn()
            if outer:
                self._local.in_tx = True
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    self._local.in_tx = False
            else:
                yield conn

    # -------------------------------------------------------------- helpers
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn().execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn().execute(sql, params).fetchone()


def open_storage(db_path: str | None = None) -> Storage:
    """Create storage at *db_path* or ``CAMPAIGN_DB`` env or in-memory."""
    import os

    path = db_path or os.environ.get("CAMPAIGN_DB") or ":memory:"
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Storage(path)
