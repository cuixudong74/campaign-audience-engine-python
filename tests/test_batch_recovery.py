import pytest

from app.errors import QuotaExhaustedError
from app.api import Services
from app.storage import Storage


def _fresh_active(path, total_quota=4, per_user_quota=1):
    from datetime import datetime, timedelta, timezone
    svc = Services(Storage(path))
    c = svc.campaigns.create_campaign(
        "c", total_quota, per_user_quota,
        start_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    svc.campaigns.activate_due()
    return svc, c.id


def test_submit_is_idempotent(services, active_campaign):
    j1 = services.batch.submit(active_campaign.id, "k", ["u1", "u2"])
    j2 = services.batch.submit(active_campaign.id, "k", ["u1", "u2"])
    assert j1["id"] == j2["id"]


def test_resume_after_midway_crash(services, active_campaign):
    cid = active_campaign.id
    # quota 4 for 6 users, per-user 1
    services.campaigns  # active_campaign has quota 100; make tight one
    job = services.batch.submit(cid, "job-1",
                                [f"u{i}" for i in range(6)])
    with pytest.raises(RuntimeError):
        services.batch.run(job["id"], fail_after=3)
    mid = services.batch.get(job["id"])
    assert mid["done"] == 3 and mid["next_offset"] == 3
    # no partial write beyond checkpoint
    assert services.campaigns.get(cid).issued_count == 3
    final = services.batch.resume(job["id"])
    assert final["done"] == 6 and final["failed"] == 0
    assert services.campaigns.get(cid).issued_count == 6


def test_resume_from_new_process_instance(file_db):
    svc, cid = _fresh_active(file_db, total_quota=3, per_user_quota=1)
    job = svc.batch.submit(cid, "job-x", ["u1", "u2", "u3", "u4", "u5"])
    with pytest.raises(RuntimeError):
        svc.batch.run(job["id"], fail_after=2)
    svc.shutdown()

    # simulate restart: continue from last committed offset
    svc2 = Services(Storage(file_db))
    final = svc2.batch.resume(job["id"])
    assert final["done"] == 3 and final["failed"] == 2
    assert final["items"][3]["error"] == "quota_exhausted"
    assert svc2.campaigns.get(cid).issued_count == 3
    svc2.shutdown()


def test_no_duplicate_issuance_on_retry(file_db):
    svc, cid = _fresh_active(file_db, total_quota=10, per_user_quota=2)
    users = ["a", "b", "c"]
    job = svc.batch.submit(cid, "dup", users)
    with pytest.raises(RuntimeError):
        svc.batch.run(job["id"], fail_after=1)
    svc.shutdown()
    svc2 = Services(Storage(file_db))
    svc2.batch.resume(job["id"])
    rows = svc2.storage.query(
        "SELECT user_id, COUNT(*) n FROM vouchers WHERE campaign_id=?"
        " GROUP BY user_id", (cid,))
    assert {r["user_id"]: r["n"] for r in rows} == {"a": 1, "b": 1, "c": 1}
    # running again changes nothing
    again = svc2.batch.run(job["id"])
    assert again["done"] == 3
    assert svc2.campaigns.get(cid).issued_count == 3
    svc2.shutdown()
