"""批量发放：检查点与断点续跑。

- 任务创建时把目标用户列表物化为 batch_items（seq 即游标）；
- 每个用户的发放在独立事务中提交，成功后推进 committed 游标；
- 中途失败：已提交部分保留且各自完整（单条事务），不会半写；
  任务标记 failed 并记录原因；
- resume 从 committed 游标继续，发放幂等键由 (batch_id, seq) 派生，
  重跑同一位置不会产生重复凭证。
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Optional

from .campaigns import now_iso
from .errors import BizError, ErrorCode
from .vouchers import issue_safe


def create_batch(conn: sqlite3.Connection, campaign_id: str,
                 user_ids: list[str]) -> str:
    from .db import tx
    bid = uuid.uuid4().hex
    now = now_iso()
    with tx(conn):
        conn.execute(
            "INSERT INTO batch_jobs VALUES (?,?,?,?,0,NULL,?,?)",
            (bid, campaign_id, "running", len(user_ids), now, now))
        conn.executemany(
            "INSERT INTO batch_items VALUES (?,?,?,'pending')",
            [(bid, i, uid) for i, uid in enumerate(user_ids)])
    return bid


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise BizError(ErrorCode.VALIDATION, f"batch {batch_id} not found", 404)
    return dict(row)


def _process_from(conn: sqlite3.Connection, batch_id: str, offset: int) -> None:
    """从 offset 游标开始逐条发放，每条独立事务。"""
    job = get_batch(conn, batch_id)
    campaign_id = job["campaign_id"]
    items = conn.execute(
        "SELECT seq, user_id FROM batch_items WHERE batch_id=? AND seq>=? ORDER BY seq",
        (batch_id, offset)).fetchall()
    for item in items:
        seq, uid = item["seq"], item["user_id"]
        idem = f"batch:{batch_id}:{seq}"  # 确定性幂等键：重跑不重复发放
        try:
            _, _created = issue_safe(conn, campaign_id, uid, idem, batch_id=batch_id)
            status = "committed"
        except BizError as e:
            if e.code in (ErrorCode.PER_USER_LIMIT_REACHED,):
                status = "skipped"  # 业务跳过，不阻断整批
            else:
                _mark_failed(conn, batch_id, seq, f"{e.code}: {e.message}")
                raise
        from .db import tx
        with tx(conn):
            conn.execute(
                "UPDATE batch_items SET status=? WHERE batch_id=? AND seq=?",
                (status, batch_id, seq))
            conn.execute(
                "UPDATE batch_jobs SET committed=?, updated_at=? WHERE id=?",
                (seq + 1, now_iso(), batch_id))


def _mark_failed(conn: sqlite3.Connection, batch_id: str, seq: int, error: str) -> None:
    from .db import tx
    with tx(conn):
        conn.execute(
            "UPDATE batch_jobs SET status='failed', committed=?, last_error=?,"
            " updated_at=? WHERE id=?",
            (seq, error, now_iso(), batch_id))


def run_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    """执行（或续跑）批量任务。失败抛错，任务状态落库可恢复。"""
    job = get_batch(conn, batch_id)
    if job["status"] == "completed":
        return job
    _process_from(conn, batch_id, job["committed"])
    from .db import tx
    with tx(conn):
        conn.execute(
            "UPDATE batch_jobs SET status='completed', updated_at=? WHERE id=?",
            (now_iso(), batch_id))
    return get_batch(conn, batch_id)


def resume_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    """从上次 committed 游标继续执行失败的任务。"""
    job = get_batch(conn, batch_id)
    if job["status"] not in ("failed", "running"):
        return job
    from .db import tx
    with tx(conn):
        conn.execute(
            "UPDATE batch_jobs SET status='running', updated_at=? WHERE id=?",
            (now_iso(), batch_id))
    return run_batch(conn, batch_id)
