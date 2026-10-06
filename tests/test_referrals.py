from datetime import datetime, timedelta, timezone

import pytest

from app.errors import ReferralRejectedError


def _t(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def test_self_referral_rejected(services):
    with pytest.raises(ReferralRejectedError) as ei:
        services.referrals.register("a", "a", 1)
    assert "self_referral" in str(ei.value)


def test_direct_cycle_rejected(services):
    services.referrals.register("a", "b", 1)
    with pytest.raises(ReferralRejectedError) as ei:
        services.referrals.register("b", "a", 1)
    assert "cyclic_referral" in str(ei.value)


def test_indirect_cycle_rejected(services):
    for src, dst in [("a", "b"), ("b", "c"), ("c", "d")]:
        services.referrals.register(src, dst, 1)
    with pytest.raises(ReferralRejectedError):
        services.referrals.register("d", "a", 1)
    # acyclic edge to a fresh user is fine
    assert services.referrals.register("d", "z", 1)["status"] == "valid"


def test_register_idempotent(services):
    r1 = services.referrals.register("a", "b", 1)
    r2 = services.referrals.register("a", "b", 1)
    assert r1["referral_id"] == r2["referral_id"]


def test_out_of_order_and_late_conversions(services):
    # conversion arrives before registration is known (event time earlier)
    late = services.referrals.record_conversion(
        "e0", "u", 1, _t(1))
    assert late["attributed"] is False and late["reason"] == "no_referral"
    services.referrals.register("r", "u", 1)
    # later event first, earlier event second (arrival disorder)
    later = services.referrals.record_conversion("e2", "u", 1, _t(3))
    earlier = services.referrals.record_conversion("e1", "u", 1, _t(2))
    assert later["attributed"] is True
    assert earlier["attributed"] is False
    assert earlier["reason"] == "referral_already_attributed"


def test_referral_attributed_only_once(services):
    services.referrals.register("r", "u", 2)
    assert services.referrals.record_conversion(
        "e1", "u", 2, _t(1))["attributed"] is True
    second = services.referrals.record_conversion("e2", "u", 2, _t(2))
    assert second["attributed"] is False
    assert second["reason"] == "referral_already_attributed"


def test_duplicate_event_replay(services):
    services.referrals.register("r", "u", 3)
    first = services.referrals.record_conversion("e", "u", 3, _t(1))
    replay = services.referrals.record_conversion("e", "u", 3, _t(1))
    assert replay["duplicate"] is True
    assert replay["attributed"] == first["attributed"] is True
