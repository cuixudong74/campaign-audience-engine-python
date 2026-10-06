"""推荐关系归因。

- 关系唯一：一个被推荐人（referee）只能归属一个推荐人，重复登记幂等返回；
- 自推/环：登记关系时沿 referee->referrer 链向上走查，命中即拒绝；
- 归因唯一：attributions 对 event_id 主键、对 (referrer, referee) 唯一，
  同一次推荐只产生一条归因，同一事件只归因一次；
- 乱序/迟到：事件先于关系到达时，关系登记后补归因（取最早未归因事件）；
  关系先于事件到达时，事件到达即归因。两种顺序结果一致。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .campaigns import now_iso
from .errors import BizError, ErrorCode


def _find_root_chain(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """沿 referee->referrer 链向上，返回链上所有用户（含起点）。"""
    chain = [user_id]
    cur = user_id
    while True:
        row = conn.execute(
            "SELECT referrer_id FROM referral_links WHERE referee_id=?", (cur,)).fetchone()
        if row is None:
            return chain
        cur = row["referrer_id"]
        if cur in chain:  # 数据已存在环，防御性截断
            return chain
        chain.append(cur)


def register_referral(
    conn: sqlite3.Connection, referrer_id: str, referee_id: str
) -> tuple[bool, str]:
    """登记推荐关系。返回 (created, existing_referrer)。"""
    from .db import tx
    if referrer_id == referee_id:
        raise BizError(ErrorCode.REFERRAL_SELF, "self referral is not allowed", 409)
    with tx(conn):
        row = conn.execute(
            "SELECT referrer_id FROM referral_links WHERE referee_id=?",
            (referee_id,)).fetchone()
        if row is not None:
            if row["referrer_id"] == referrer_id:
                return False, referrer_id  # 幂等重放
            raise BizError(ErrorCode.REFERRAL_DUPLICATE,
                           f"referee {referee_id} already attributed", 409)
        # 环检测：若 referee 是 referrer 的上游，则成环
        if referee_id in _find_root_chain(conn, referrer_id):
            raise BizError(ErrorCode.REFERRAL_CYCLE,
                           f"referral cycle detected via {referee_id}", 409)
        conn.execute(
            "INSERT INTO referral_links VALUES (?,?,?)",
            (referee_id, referrer_id, now_iso()))
        _backfill_attribution(conn, referrer_id, referee_id)
    return True, referrer_id


def _attribute(conn: sqlite3.Connection, event_id: str,
               referrer_id: str, referee_id: str) -> bool:
    """插入归因，唯一约束保证同事件/同推荐只成功一次。返回是否新建。"""
    try:
        conn.execute(
            "INSERT INTO attributions VALUES (?,?,?,?)",
            (event_id, referrer_id, referee_id, now_iso()))
        return True
    except sqlite3.IntegrityError:
        return False


def _backfill_attribution(conn: sqlite3.Connection,
                          referrer_id: str, referee_id: str) -> None:
    """关系建立时，补归因该被推荐人最早到达且未归因的转化事件。"""
    row = conn.execute(
        """SELECT e.event_id FROM conversion_events e
           WHERE e.user_id=? AND NOT EXISTS (
               SELECT 1 FROM attributions a WHERE a.event_id=e.event_id)
           ORDER BY e.occurred_at ASC, e.event_id ASC LIMIT 1""",
        (referee_id,)).fetchone()
    if row is not None:
        _attribute(conn, row["event_id"], referrer_id, referee_id)


def record_conversion(
    conn: sqlite3.Connection, event_id: str, user_id: str, occurred_at: str
) -> tuple[bool, Optional[str]]:
    """记录转化事件并尝试归因。返回 (event_created, attributed_event_id)。"""
    from .db import tx
    with tx(conn):
        row = conn.execute(
            "SELECT event_id FROM conversion_events WHERE event_id=?", (event_id,)).fetchone()
        if row is not None:
            # 事件重复到达：幂等，返回已有归因
            att = conn.execute(
                "SELECT event_id FROM attributions WHERE event_id=?", (event_id,)).fetchone()
            return False, (att["event_id"] if att else None)
        conn.execute(
            "INSERT INTO conversion_events VALUES (?,?,?,?)",
            (event_id, user_id, occurred_at, now_iso()))
        link = conn.execute(
            "SELECT referrer_id FROM referral_links WHERE referee_id=?",
            (user_id,)).fetchone()
        if link is None:
            return True, None
        # 该推荐关系是否已归因过：同一次推荐只归因一次
        existing = conn.execute(
            "SELECT event_id FROM attributions WHERE referrer_id=? AND referee_id=?",
            (link["referrer_id"], user_id)).fetchone()
        if existing is not None:
            return True, existing["event_id"]
        _attribute(conn, event_id, link["referrer_id"], user_id)
        return True, event_id


def list_attributions(conn: sqlite3.Connection, referrer_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM attributions WHERE referrer_id=? ORDER BY attributed_at, event_id",
        (referrer_id,)).fetchall()
    return [dict(r) for r in rows]
