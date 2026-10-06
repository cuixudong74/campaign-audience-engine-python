from datetime import datetime, timedelta, timezone

from app.api import Services
from app.storage import Storage


def test_status_and_schedule_survive_restart(file_db):
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    svc1 = Services(Storage(file_db))
    c = svc1.campaigns.create_campaign("scheduled-c", 10, 2, start_at=start)
    assert svc1.campaigns.get(c.id).status.value == "scheduled"
    svc1.shutdown()

    # reopen with a fresh process-like instance: state must not be reset
    svc2 = Services(Storage(file_db))
    assert svc2.campaigns.get(c.id).status.value == "scheduled"
    # due activation still works with the persisted start_at
    due = svc2.campaigns.activate_due(start + timedelta(seconds=1))
    assert due == [c.id]
    assert svc2.campaigns.get(c.id).status.value == "active"
    svc2.shutdown()



def test_vouchers_survive_restart(file_db):
    start = datetime.now(timezone.utc) - timedelta(seconds=5)
    svc1 = Services(Storage(file_db))
    c = svc1.campaigns.create_campaign("c", 10, 2, start_at=start)
    svc1.campaigns.activate_due()
    v = svc1.vouchers.issue(c.id, "u1", "req-1")
    svc1.shutdown()

    svc2 = Services(Storage(file_db))
    got = svc2.vouchers.get(v.id)
    assert got.user_id == "u1" and got.status.value == "issued"
    # duplicate request after restart still cannot double issue
    again = svc2.vouchers.issue(c.id, "u1", "req-1")
    assert again.id == v.id
    assert svc2.campaigns.get(c.id).issued_count == 1
    svc2.shutdown()
