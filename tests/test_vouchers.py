import threading

import pytest

from app.authz import Actor
from app.errors import (ConflictError, InvalidTransitionError,
                        PerUserLimitError, PermissionDeniedError, QuotaExhaustedError,
                        VoucherNotRedeemableError)
from app.models import Role


def test_duplicate_issue_request_idempotent(services, active_campaign):
    cid = active_campaign.id
    v1 = services.vouchers.issue(cid, "u1", "same-request")
    v2 = services.vouchers.issue(cid, "u1", "same-request")
    assert v1.id == v2.id
    assert services.campaigns.get(cid).issued_count == 1


def test_request_id_reuse_with_different_payload_conflict(services, active_campaign):
    cid = active_campaign.id
    services.vouchers.issue(cid, "u1", "req")
    with pytest.raises(ConflictError):
        services.vouchers.issue(cid, "u2", "req")


def test_total_quota_no_oversell_concurrent(file_db):
    """Multiple threads racing for remaining quota never oversell."""
    from app.api import Services
    from app.storage import Storage
    from datetime import datetime, timedelta, timezone
    svc = Services(Storage(file_db))
    c = svc.campaigns.create_campaign(
        "race", total_quota=20, per_user_quota=20,
        start_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    svc.campaigns.activate_due()
    svc.shutdown()

    results = []
    errors = []

    def worker(worker_id: int):
        local = Services(Storage(file_db))
        try:
            for i in range(5):
                try:
                    v = local.vouchers.issue(
                        c.id, f"w{worker_id}-u{i}", f"req-{worker_id}-{i}")
                    results.append(v.id)
                except (QuotaExhaustedError, PerUserLimitError):
                    errors.append("quota")
        finally:
            local.shutdown()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    verify = Services(Storage(file_db))
    assert verify.campaigns.get(c.id).issued_count == 20
    assert len(set(results)) == len(results) == 20
    assert len(errors) == 30
    verify.shutdown()


def test_per_user_limit(services, active_campaign):
    cid = active_campaign.id
    services.vouchers.issue(cid, "u1", "r1")
    services.vouchers.issue(cid, "u1", "r2")
    with pytest.raises(PerUserLimitError):
        services.vouchers.issue(cid, "u1", "r3")


def test_issue_only_when_active(services):
    c = services.campaigns.create_campaign("draft-c", 5, 1)
    with pytest.raises(InvalidTransitionError) as ei:
        services.vouchers.issue(c.id, "u1", "r1")
    assert ei.value.code == "campaign_not_active"


def test_redeem_once_and_idempotent(services, active_campaign):
    cid = active_campaign.id
    v = services.vouchers.issue(cid, "u1", "r1")
    got = services.vouchers.redeem(v.id, "rv-1", expected_user_id="u1")
    assert got.status.value == "redeemed"
    # same request id -> replay, no second redemption
    again = services.vouchers.redeem(v.id, "rv-1", expected_user_id="u1")
    assert again.id == v.id
    # different request -> voucher no longer redeemable
    with pytest.raises(VoucherNotRedeemableError):
        services.vouchers.redeem(v.id, "rv-2", expected_user_id="u1")


def test_concurrent_double_redeem_only_one_wins(file_db):
    from app.api import Services
    from app.storage import Storage
    from datetime import datetime, timedelta, timezone
    svc = Services(Storage(file_db))
    c = svc.campaigns.create_campaign(
        "rc", 5, 1,
        start_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    svc.campaigns.activate_due()
    v = svc.vouchers.issue(c.id, "u1", "i1")
    svc.shutdown()

    outcomes = []

    def worker(tag):
        local = Services(Storage(file_db))
        try:
            try:
                local.vouchers.redeem(v.id, f"redeem-{tag}",
                                      expected_user_id="u1")
                outcomes.append("ok")
            except VoucherNotRedeemableError:
                outcomes.append("rejected")
        finally:
            local.shutdown()

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok", "rejected"]


def test_replay_redeem_request_for_other_user_denied(services, active_campaign):
    cid = active_campaign.id
    v = services.vouchers.issue(cid, "owner", "own-req")
    # legitimate owner redeem
    services.vouchers.redeem(v.id, "redeem-key", expected_user_id="owner")
    # another user attempts replay with same request key
    with pytest.raises(PermissionDeniedError):
        services.vouchers.redeem(v.id, "redeem-key",
                                 expected_user_id="intruder")
