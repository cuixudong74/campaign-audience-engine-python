"""SQLite 持久化层。

并发策略：
- WAL 模式 + busy_timeout，读写不互斥；
- 所有多步写操作放在 BEGIN IMMEDIATE 事务里，保证原子性；
- 额度扣减用条件 UPDATE 的 affected-rows 判定，避免超发。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    scheduled_at TEXT,
    total_quota INTEGER,
    per_user_limit INTEGER NOT NULL DEFAULT 1,
    audience_rule_id TEXT,
    audience_rule_version INTEGER,
    issued_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audience_rules (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);

CREATE TABLE IF NOT EXISTS vouchers (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    rule_version INTEGER,
    issued_at TEXT NOT NULL,
    redeemed_at TEXT,
    redeem_idempotency_key TEXT UNIQUE,
    batch_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_vouchers_campaign_user ON vouchers(campaign_id, user_id);

CREATE TABLE IF NOT EXISTS referral_links (
    referee_id TEXT PRIMARY KEY,     -- 一个被推荐人只能归属一个推荐人
    referrer_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_referral_referrer ON referral_links(referrer_id);

CREATE TABLE IF NOT EXISTS conversion_events (
    event_id TEXT PRIMARY KEY,       -- 事件幂等：重复到达只记录一次
    user_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attributions (
    event_id TEXT PRIMARY KEY,       -- 同一事件只归因一次
    referrer_id TEXT NOT NULL,
    referee_id TEXT NOT NULL,
    attributed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_attribution_pair
    ON attributions(referrer_id, referee_id);  -- 同一次推荐只归因一次

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    status TEXT NOT NULL,            -- running / completed / failed
    total INTEGER NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,   -- 已提交游标：断点续跑位置
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,            -- 用户列表中的序号，即游标
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending / committed / skipped
    PRIMARY KEY (batch_id, seq)
);
"""

_local = threading.local()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """IMMEDIATE 事务：进入即取写锁，异常自动回滚，保证多步写原子。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
