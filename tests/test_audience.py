import pytest

from app.rule_engine import (BehaviorEvent, UserProfile, evaluate,
                             rule_fingerprint, selection_hash, validate_rule)
from app.errors import RuleValidationError


def test_rule_is_deterministic(services):
    rule = {"op": "AND", "children": [
        {"field": "attr.city", "op": "eq", "value": "BJ"},
        {"event": "buy", "op": "count_gte", "value": 1}]}
    users = [
        UserProfile("u1", {"city": "BJ"},
                    (BehaviorEvent("buy", "2025-01-01T00:00:00Z", 10.0),)),
        UserProfile("u2", {"city": "BJ"}, ()),
        UserProfile("u3", {"city": "SH"},
                    (BehaviorEvent("buy", "2025-01-01T00:00:00Z", 1.0),)),
    ]
    c = services.campaigns.create_campaign("c", 10, 2)
    services.audience.upsert_rule(c.id, rule)
    s1 = services.audience.run_selection(c.id, users)
    s2 = services.audience.run_selection(c.id, list(reversed(users)))
    assert s1["matched"] == s2["matched"] == ["u1"]
    assert s1["selection_hash"] == s2["selection_hash"]


def test_rule_change_creates_version_keeps_history(services, active_campaign):
    cid = active_campaign.id
    r1_body = {"field": "attr.city", "op": "eq", "value": "BJ"}
    r1 = services.audience.upsert_rule(cid, r1_body)
    users = [UserProfile("u1", {"city": "BJ"}),
             UserProfile("u2", {"city": "SH"})]
    snap1 = services.audience.run_selection(cid, users)
    # vouchers issued against v1
    v = services.vouchers.issue(cid, "u1", "vr1")
    assert v.rule_version == 1
    # edit rule -> v2
    r2 = services.audience.upsert_rule(cid,
                                       {"field": "attr.city", "op": "eq",
                                        "value": "SH"})
    assert r2.version == 2 and r1.fingerprint != r2.fingerprint
    snap2 = services.audience.run_selection(cid, users)
    assert snap2["matched"] == ["u2"]
    # historical snapshot and issued voucher remain intact & explainable
    old = services.audience.get_snapshot(cid, 1)
    assert old["matched"] == ["u1"]
    assert services.vouchers.get(v.id).rule_version == 1
    ex = services.audience.explain(cid, 1, users[0])
    assert ex["matched"] is True
    ex2 = services.audience.explain(cid, 2, users[0])
    assert ex2["matched"] is False


def test_identical_body_no_new_version(services):
    c = services.campaigns.create_campaign("c", 10, 2)
    body = {"field": "attr.age", "op": "gte", "value": 18}
    r1 = services.audience.upsert_rule(c.id, body)
    r2 = services.audience.upsert_rule(c.id, {"field": "attr.age",
                                             "op": "gte", "value": 18})
    assert r1.version == r2.version


def test_invalid_rules_rejected():
    with pytest.raises(RuleValidationError):
        validate_rule({"op": "AND"})
    with pytest.raises(RuleValidationError):
        validate_rule({"field": "attr.x", "op": "weird", "value": 1})
    with pytest.raises(RuleValidationError):
        validate_rule({"op": "NOT"})




def test_issue_enforces_audience_rule(services, active_campaign):
    from app.errors import InvalidTransitionError
    cid = active_campaign.id
    services.audience.upsert_rule(
        cid, {"field": "attr.tier", "op": "eq", "value": "vip"})
    vip = UserProfile("u1", {"tier": "vip"})
    normal = UserProfile("u2", {"tier": "normal"})
    v = services.vouchers.issue(cid, "u1", "targeted-1", profile=vip)
    assert v.rule_version == 1
    with pytest.raises(InvalidTransitionError) as ei:
        services.vouchers.issue(cid, "u2", "targeted-2", profile=normal)
    assert ei.value.code == "not_in_audience"
    # without a profile the targeting check is skipped (channel-supplied
    # issuance decides eligibility upstream), quota still applies
    v3 = services.vouchers.issue(cid, "u3", "targeted-3")
    assert v3.id is not None
