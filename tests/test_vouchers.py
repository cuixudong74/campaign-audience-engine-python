"""凭证：幂等、并发争抢不超发、核销不重复、限领。"""
import threading

from app.campaigns import create_campaign, transition
from app.db import connect
from app.errors import BizError, ErrorCode
from app.models import CampaignStatus
from app.vouchers import issue_safe, redeem


def _active_campaign(conn, **kw):
    c = create_campaign(conn, "p", **kw)
    transition(conn, c.id, CampaignStatus.ACTIVE)
    return c


def test_issue_idempotent_replay(conn):
    c = _active_campaign(conn)
    v1, created1 = issue_safe(conn, c.id, "u1", "key-1")
    v2, created2 = issue_safe(conn, c.id, "u1", "key-1")
    assert created1 and not created2 and v1.id == v2.id
    n = conn.execute("SELECT COUNT(*) c FROM vouchers").fetchone()["c"]
    assert n == 1


def test_redeem_idempotent_and_no_double_redeem(conn):
    c = _active_campaign(conn)
    v, _ = issue_safe(conn, c.id, "u1", "k")
    _, now1 = redeem(conn, v.id, "r1")
    _, now2 = redeem(conn, v.id, "r1")          # 同一请求重放
    assert now1 and not now2
    try:
        redeem(conn, v.id, "r2")                # 不同请求核销同一凭证
        assert False
    except BizError as e:
        assert e.code == ErrorCode.ALREADY_REDEEMED


def test_per_user_limit(conn):
    c = _active_campaign(conn, per_user_limit=2, total_quota=10)
    issue_safe(conn, c.id, "u1", "k1")
    issue_safe(conn, c.id, "u1", "k2")
    try:
        issue_safe(conn, c.id, "u1", "k3")
        assert False
    except BizError as e:
        assert e.code == ErrorCode.PER_USER_LIMIT_REACHED


def test_concurrent_quota_no_oversell(db_path):
    setup = connect(db_path)
    c = _active_campaign(setup, total_quota=5, per_user_limit=100)
    cid = c.id
    setup.close()
    results, lock = [], threading.Lock()

    def worker(i):
        conn = connect(db_path)
        try:
            issue_safe(conn, cid, f"user-{i}", f"key-{i}")
            with lock:
                results.append("ok")
        except BizError as e:
            with lock:
                results.append(e.code)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 5
    assert results.count(ErrorCode.QUOTA_EXHAUSTED) == 15
    check = connect(db_path)
    row = check.execute(
        "SELECT issued_count, total_quota FROM campaigns WHERE id=?", (cid,)).fetchone()
    assert row["issued_count"] == 5  # 精确等于额度，不超发
    assert check.execute("SELECT COUNT(*) c FROM vouchers").fetchone()["c"] == 5
    check.close()


def test_concurrent_same_idempotency_key(db_path):
    setup = connect(db_path)
    c = _active_campaign(setup, total_quota=10)
    cid = c.id
    setup.close()
    outcomes, lock = [], threading.Lock()

    def worker():
        conn = connect(db_path)
        try:
            v, created = issue_safe(conn, cid, "u1", "same-key")
            with lock:
                outcomes.append((v.id, created))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({vid for vid, _ in outcomes}) == 1          # 只有一张凭证
    assert sum(1 for _, created in outcomes if created) == 1
    check = connect(db_path)
    assert check.execute("SELECT COUNT(*) c FROM vouchers").fetchone()["c"] == 1
    check.close()


def test_issue_rejected_when_not_active(conn):
    c = create_campaign(conn, "p")  # draft
    try:
        issue_safe(conn, c.id, "u1", "k")
        assert False
    except BizError as e:
        assert e.code == ErrorCode.CAMPAIGN_NOT_ACTIVE
