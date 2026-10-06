"""人群规则：组合条件、确定性、版本化与可解释性。"""
from datetime import datetime, timedelta, timezone

from app.audience import (create_rule, evaluate, explain, get_rule, select,
                          update_rule)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _users():
    recent = (NOW - timedelta(days=5)).isoformat()
    old = (NOW - timedelta(days=60)).isoformat()
    return {
        "u1": {"attrs": {"age": 25, "city": "bj"},
               "behaviors": {"purchase": [recent, recent, recent]}},
        "u2": {"attrs": {"age": 17, "city": "bj"}, "behaviors": {}},
        "u3": {"attrs": {"age": 40, "city": "sh"},
               "behaviors": {"purchase": [old]}},  # 行为超出时间窗
    }


def test_combined_attr_and_behavior(conn):
    rule = {"all": [
        {"attr": "age", "op": "gte", "value": 18},
        {"attr": "city", "op": "eq", "value": "bj"},
        {"behavior": "purchase", "op": "gte", "value": 2, "within_days": 30},
    ]}
    users = _users()
    assert select(rule, users, NOW) == ["u1"]
    # 确定性：同规则同数据多次求值结果完全一致
    for _ in range(5):
        assert select(rule, users, NOW) == select(rule, users, NOW)


def test_any_and_not(conn):
    rule = {"any": [
        {"attr": "city", "op": "eq", "value": "sh"},
        {"not": {"attr": "age", "op": "gte", "value": 18}},
    ]}
    assert select(rule, _users(), NOW) == ["u2", "u3"]


def test_explain_miss_reason(conn):
    rule = {"all": [{"attr": "age", "op": "gte", "value": 18},
                    {"behavior": "purchase", "op": "gte", "value": 1,
                     "within_days": 30}]}
    exp = explain(rule, _users()["u3"], NOW)
    assert exp["matched"] is False
    leaf = exp["trace"]["all"][1]
    assert leaf["behavior"] == "purchase" and leaf["actual_count"] == 0


def test_rule_versioning_keeps_old_results_explainable(conn):
    rid, v1 = create_rule(conn, "r", {"attr": "age", "op": "gte", "value": 18})
    users = _users()
    old_result = select(get_rule(conn, rid, v1)[1], users, NOW)
    v2 = update_rule(conn, rid, {"attr": "age", "op": "gte", "value": 30})
    assert v2 == 2
    new_result = select(get_rule(conn, rid)[1], users, NOW)
    assert old_result == ["u1", "u3"] and new_result == ["u3"]
    # 旧版本仍可取出解释历史结果
    assert get_rule(conn, rid, 1)[1] == {"attr": "age", "op": "gte", "value": 18}
