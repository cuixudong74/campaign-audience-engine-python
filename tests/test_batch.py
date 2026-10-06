"""批量发放：中途失败不留部分写入、断点续跑不重复。"""
import pytest

from app.batch import (create_batch, get_batch, resume_batch, run_batch)
from app.campaigns import create_campaign, transition
from app.errors import BizError, ErrorCode
from app.models import CampaignStatus


def _active(conn, **kw):
    c = create_campaign(conn, "p", **kw)
    transition(conn, c.id, CampaignStatus.ACTIVE)
    return c


def test_failure_midway_then_resume(conn):
    c = _active(conn, total_quota=3, per_user_limit=1)
    bid = create_batch(conn, c.id, ["u1", "u2", "u3", "u4", "u5"])
    with pytest.raises(BizError) as ei:
        run_batch(conn, bid)
    assert ei.value.code == ErrorCode.QUOTA_EXHAUSTED
    job = get_batch(conn, bid)
    assert job["status"] == "failed" and job["committed"] == 3
    # 已提交的 3 张完整保留，没有半写的第 4 张
    assert conn.execute("SELECT COUNT(*) c FROM vouchers").fetchone()["c"] == 3
    # 恢复：提高额度后从位置 3 继续
    conn.execute("UPDATE campaigns SET total_quota=10")
    job = resume_batch(conn, bid)
    assert job["status"] == "completed" and job["committed"] == 5
    assert conn.execute("SELECT COUNT(*) c FROM vouchers").fetchone()["c"] == 5
    # 再次 resume 幂等，不重复发放
    resume_batch(conn, bid)
    assert conn.execute("SELECT COUNT(*) c FROM vouchers").fetchone()["c"] == 5


def test_per_user_limit_skips_without_blocking_batch(conn):
    c = _active(conn, total_quota=10, per_user_limit=1)
    conn2 = conn
    from app.vouchers import issue_safe
    issue_safe(conn2, c.id, "u2", "pre-existing")  # u2 已领过
    bid = create_batch(conn, c.id, ["u1", "u2", "u3"])
    job = run_batch(conn, bid)
    assert job["status"] == "completed"
    rows = conn.execute(
        "SELECT user_id, status FROM batch_items WHERE batch_id=? ORDER BY seq",
        (bid,)).fetchall()
    statuses = {r["user_id"]: r["status"] for r in rows}
    assert statuses == {"u1": "committed", "u2": "skipped", "u3": "committed"}
