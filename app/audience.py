"""人群规则引擎。

规则定义是 JSON 树：
  {"all": [子条件...]} / {"any": [...]} / {"not": 子条件}
  叶子：{"attr": "age", "op": "gte", "value": 18}
        {"behavior": "purchase", "op": "gte", "value": 3, "within_days": 30}

确定性保证：
- 求值是纯函数，只依赖 (规则定义, 用户上下文, 参考时间)；
- 圈选按 user_id 排序遍历，输出稳定有序；
- 规则版本化存储，旧版本永远可读，发放记录绑定规则版本，可回溯解释。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .errors import BizError, ErrorCode

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
}


def _validate(node: Any) -> None:
    if not isinstance(node, dict):
        raise BizError(ErrorCode.RULE_INVALID, "rule node must be an object")
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        children = node[key]
        if not isinstance(children, list) or not children:
            raise BizError(ErrorCode.RULE_INVALID, f"'{key}' must be a non-empty list")
        for c in children:
            _validate(c)
    elif "not" in node:
        _validate(node["not"])
    elif "attr" in node:
        if node.get("op") not in _OPS or "value" not in node:
            raise BizError(ErrorCode.RULE_INVALID, "attr condition needs valid op and value")
    elif "behavior" in node:
        if node.get("op") not in _OPS or "value" not in node:
            raise BizError(ErrorCode.RULE_INVALID, "behavior condition needs valid op and value")
    else:
        raise BizError(ErrorCode.RULE_INVALID, f"unknown rule node: {node}")


def evaluate(node: dict, user_ctx: dict, now: Optional[datetime] = None) -> tuple[bool, dict]:
    """求值并返回 (结果, 解释树)。解释树记录每个节点的命中情况。"""
    now = now or datetime.now(timezone.utc)
    if "all" in node:
        parts = [evaluate(c, user_ctx, now) for c in node["all"]]
        return all(p[0] for p in parts), {"all": [p[1] for p in parts]}
    if "any" in node:
        parts = [evaluate(c, user_ctx, now) for c in node["any"]]
        return any(p[0] for p in parts), {"any": [p[1] for p in parts]}
    if "not" in node:
        ok, trace = evaluate(node["not"], user_ctx, now)
        return (not ok), {"not": trace}
    if "attr" in node:
        actual = user_ctx.get("attrs", {}).get(node["attr"])
        try:
            ok = actual is not None and _OPS[node["op"]](actual, node["value"])
        except TypeError:
            ok = False
        return ok, {"attr": node["attr"], "op": node["op"], "expect": node["value"],
                    "actual": actual, "matched": ok}
    if "behavior" in node:
        events = user_ctx.get("behaviors", {}).get(node["behavior"], [])
        within = node.get("within_days")
        if within is not None:
            cutoff = (now - timedelta(days=within)).isoformat()
            events = [e for e in events if e >= cutoff]
        count = len(events)
        ok = _OPS[node["op"]](count, node["value"])
        return ok, {"behavior": node["behavior"], "op": node["op"],
                    "expect": node["value"], "actual_count": count, "matched": ok}
    raise BizError(ErrorCode.RULE_INVALID, f"unknown rule node: {node}")


def select(rule_def: dict, users: dict[str, dict],
           now: Optional[datetime] = None) -> list[str]:
    """圈选：按 user_id 排序遍历，输出确定性的有序名单。"""
    return [uid for uid in sorted(users) if evaluate(rule_def, users[uid], now)[0]]


def explain(rule_def: dict, user_ctx: dict,
            now: Optional[datetime] = None) -> dict:
    """对单个用户给出命中/未命中的完整解释。"""
    ok, trace = evaluate(rule_def, user_ctx, now)
    return {"matched": ok, "trace": trace}


# ---- 规则版本化存储 ----

def create_rule(conn: sqlite3.Connection, name: str, definition: dict) -> tuple[str, int]:
    _validate(definition)
    rid = uuid.uuid4().hex
    with conn:
        conn.execute(
            "INSERT INTO audience_rules VALUES (?,?,?,?,?)",
            (rid, name, 1, json.dumps(definition, sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )
    return rid, 1


def update_rule(conn: sqlite3.Connection, rule_id: str, definition: dict) -> int:
    """修改规则产生新版本，旧版本保留可解释历史发放。"""
    _validate(definition)
    with conn:
        row = conn.execute(
            "SELECT MAX(version) v, name FROM audience_rules WHERE id=? GROUP BY name",
            (rule_id,)).fetchone()
        if row is None:
            raise BizError(ErrorCode.RULE_NOT_FOUND, f"rule {rule_id} not found", 404)
        new_version = row["v"] + 1
        conn.execute(
            "INSERT INTO audience_rules VALUES (?,?,?,?,?)",
            (rule_id, row["name"], new_version, json.dumps(definition, sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )
    return new_version


def get_rule(conn: sqlite3.Connection, rule_id: str,
             version: Optional[int] = None) -> tuple[int, dict]:
    if version is None:
        row = conn.execute(
            "SELECT version, definition_json FROM audience_rules WHERE id=? "
            "ORDER BY version DESC LIMIT 1", (rule_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT version, definition_json FROM audience_rules WHERE id=? AND version=?",
            (rule_id, version)).fetchone()
    if row is None:
        raise BizError(ErrorCode.RULE_NOT_FOUND,
                       f"rule {rule_id} v{version or 'latest'} not found", 404)
    return row["version"], json.loads(row["definition_json"])
