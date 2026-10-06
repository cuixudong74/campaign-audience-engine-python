"""活动生命周期服务。

状态机：draft -> scheduled -> active -> paused -> active ... -> ended
- 非法流转抛 BizError(INVALID_STATE_TRANSITION)，message 携带当前/目标状态；
- 预约到点自动激活：读取时惰性应用（_apply_due_activation），状态与预约时间
  都持久化在库里，重启后按当前时间重新推导，不会错乱；
- 状态变更带乐观锁（version），并发流转只有一个成功。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from .errors import BizError, ErrorCode
from .models import ALLOWED_TRANSITIONS, Campaign, CampaignStatus


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_campaign(r: sqlite3.Row) -> Campaign:
    return Campaign(
        id=r["id"], name=r["name"], status=CampaignStatus(r["status"]),
        scheduled_at=r["scheduled_at"], total_quota=r["total_quota"],
        per_user_limit=r["per_user_limit"], audience_rule_id=r["audience_rule_id"],
        version=r["version"], created_at=r["created_at"], updated_at=r["updated_at"],
    )


def create_campaign(
    conn: sqlite3.Connection,
    name: str,
    scheduled_at: Optional[str] = None,
    total_quota: Optional[int] = None,
    per_user_limit: int = 1,
    audience_rule_id: Optional[str] = None,
    audience_rule_version: Optional[int] = None,
) -> Campaign:
    if per_user_limit < 1:
        raise BizError(ErrorCode.VALIDATION, "per_user_limit must be >= 1")
    if total_quota is not None and total_quota < 1:
        raise BizError(ErrorCode.VALIDATION, "total_quota must be >= 1")
    cid = uuid.uuid4().hex
    now = now_iso()
    with conn:  # 单语句事务
        conn.execute(
            """INSERT INTO campaigns
               (id, name, status, scheduled_at, total_quota, per_user_limit,
                audience_rule_id, audience_rule_version, issued_count, version,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,0,0,?,?)""",
            (cid, name, CampaignStatus.DRAFT.value, scheduled_at, total_quota,
             per_user_limit, audience_rule_id, audience_rule_version, now, now),
        )
    return get_campaign(conn, cid)


def _get_row(conn: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        raise BizError(ErrorCode.CAMPAIGN_NOT_FOUND, f"campaign {campaign_id} not found", 404)
    return row


def _apply_due_activation(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """预约时间已到且仍在 scheduled：自动流转为 active 并持久化。"""
    if row["status"] == CampaignStatus.SCHEDULED.value and row["scheduled_at"]:
        if now_iso() >= row["scheduled_at"]:
            _do_transition(conn, row["id"], CampaignStatus.SCHEDULED, CampaignStatus.ACTIVE)


def _do_transition(
    conn: sqlite3.Connection,
    campaign_id: str,
    expect: CampaignStatus,
    target: CampaignStatus,
) -> None:
    cur = conn.execute(
        """UPDATE campaigns SET status=?, version=version+1, updated_at=?
           WHERE id=? AND status=? AND version=(
               SELECT version FROM campaigns WHERE id=?)""",
        (target.value, now_iso(), campaign_id, expect.value, campaign_id),
    )
    if cur.rowcount != 1:
        raise BizError(
            ErrorCode.CONFLICT, "concurrent state change, retry", 409)


def transition(
    conn: sqlite3.Connection, campaign_id: str, target: CampaignStatus
) -> Campaign:
    """显式状态流转。非法路径抛 INVALID_STATE_TRANSITION。"""
    from .db import tx
    with tx(conn):
        row = _get_row(conn, campaign_id)
        _apply_due_activation(conn, row)
        row = _get_row(conn, campaign_id)
        current = CampaignStatus(row["status"])
        if target not in ALLOWED_TRANSITIONS[current]:
            raise BizError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"cannot transition from {current.value} to {target.value}",
                409,
            )
        if target == CampaignStatus.ACTIVE and current == CampaignStatus.SCHEDULED:
            # 手动提前激活预约中的活动是允许的
            pass
        _do_transition(conn, campaign_id, current, target)
    return get_campaign(conn, campaign_id)


def get_campaign(conn: sqlite3.Connection, campaign_id: str) -> Campaign:
    from .db import tx
    with tx(conn):
        row = _get_row(conn, campaign_id)
        _apply_due_activation(conn, row)
        row = _get_row(conn, campaign_id)
    return _row_to_campaign(row)


def activate_due_campaigns(conn: sqlite3.Connection) -> int:
    """供定时任务调用：把所有到点的预约活动置为 active。返回激活数量。"""
    from .db import tx
    with tx(conn):
        rows = conn.execute(
            "SELECT id FROM campaigns WHERE status=? AND scheduled_at IS NOT NULL AND scheduled_at<=?",
            (CampaignStatus.SCHEDULED.value, now_iso()),
        ).fetchall()
        for r in rows:
            _do_transition(conn, r["id"], CampaignStatus.SCHEDULED, CampaignStatus.ACTIVE)
    return len(rows)
