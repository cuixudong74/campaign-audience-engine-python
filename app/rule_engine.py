"""Deterministic audience rule model and evaluation engine.

Rule DSL (JSON-serialisable dict)::

    {"op": "AND", "children": [
        {"op": "AND", "children": [...]},
        {"op": "OR",  "children": [...]},
        {"op": "NOT", "child": {...}},
        {"field": "attr.city", "op": "eq", "value": "Beijing"},
        {"event": "purchase", "op": "count_gte", "value": 2,
         "since": "2024-01-01T00:00:00Z", "min_amount": 100.0},
    ]}

Evaluation is a pure function of the rule body and the user data, so the
same rule + data always produces the same result.  ``rule_fingerprint``
canonicalises the body (sorted keys, no whitespace) before hashing, which
makes semantic edits detectable while ignoring formatting noise.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime
from typing import Any

from .errors import RuleValidationError

_SCALAR_OPS = {"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"}
_AGG_OPS = {"count", "count_gte", "count_lte", "sum_amount",
            "sum_amount_gte", "last_within_days", "ever", "never"}
_ALL_OPS = _SCALAR_OPS | _AGG_OPS
_AGGREGATE_FIELDS = {"attr.age", "attr.signup_days", "event.*"}


@dataclasses.dataclass(frozen=True)
class BehaviorEvent:
    event_type: str
    occurred_at: str  # ISO-8601 UTC
    amount: float = 0.0


@dataclasses.dataclass(frozen=True)
class UserProfile:
    user_id: str
    attributes: dict[str, Any]
    events: tuple[BehaviorEvent, ...] = ()


# ---------------------------------------------------------------- canonical
def canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def rule_fingerprint(body: dict) -> str:
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()[:16]


def selection_hash(rule_version: int, fingerprint: str,
                   user_ids: list[str]) -> str:
    """Deterministic digest of a concrete selection result."""
    joined = "\n".join(sorted(user_ids))
    payload = f"{rule_version}|{fingerprint}|{joined}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# -------------------------------------------------------------- validation
def validate_rule(body: dict) -> None:
    if not isinstance(body, dict):
        raise RuleValidationError("rule body must be an object")
    _validate_node(body, path="$")


def _validate_node(node: Any, path: str) -> None:
    if not isinstance(node, dict):
        raise RuleValidationError(f"{path}: node must be an object")
    op = node.get("op")
    if op in ("AND", "OR"):
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise RuleValidationError(f"{path}: {op} needs non-empty children")
        for i, child in enumerate(children):
            _validate_node(child, f"{path}.{op}[{i}]")
    elif op == "NOT":
        if not isinstance(node.get("child"), dict):
            raise RuleValidationError(f"{path}: NOT needs a child node")
        _validate_node(node["child"], f"{path}.NOT")
    elif "field" in node:
        if op not in _SCALAR_OPS:
            raise RuleValidationError(
                f"{path}: unsupported op {op!r} for field condition")
        if not isinstance(node["field"], str) or not node["field"]:
            raise RuleValidationError(f"{path}: field must be a non-empty string")
        if "value" not in node:
            raise RuleValidationError(f"{path}: value is required")
    elif "event" in node:
        if op not in _AGG_OPS:
            raise RuleValidationError(
                f"{path}: unsupported op {op!r} for event condition")
        if not isinstance(node["event"], str) or not node["event"]:
            raise RuleValidationError(f"{path}: event must be a non-empty string")
        if op in {"count", "count_gte", "count_lte", "ever", "never"}:
            pass  # value optional / required depending
        if op in {"count_gte", "count_lte", "sum_amount_gte", "last_within_days"}:
            if not isinstance(node.get("value"), (int, float)):
                raise RuleValidationError(f"{path}: numeric value required")
    else:
        raise RuleValidationError(
            f"{path}: node must be a logical group, field or event condition")


# ------------------------------------------------------------- evaluation
def evaluate(body: dict, user: UserProfile) -> bool:
    return bool(_eval_node(body, user))


def explain(body: dict, user: UserProfile) -> dict:
    """Return a structured trace: result of every node."""
    return _eval_node(body, user, explain=True)


def _eval_node(node: dict, user: UserProfile, explain: bool = False) -> Any:
    op = node["op"]
    if op in ("AND", "OR"):
        parts = [_eval_node(c, user, explain) for c in node["children"]]
        result = all(parts) if op == "AND" else any(parts)
        return {"op": op, "result": bool(result), "children": parts} if explain else result
    if op == "NOT":
        part = _eval_node(node["child"], user, explain)
        result = not (part["result"] if explain else part)
        return {"op": "NOT", "result": bool(result), "child": part} if explain else result
    if "field" in node:
        result = _eval_field(node, user)
        if explain:
            return {"op": op, "field": node["field"],
                    "actual": _attr_value(user, node["field"]),
                    "expected": node.get("value"), "result": bool(result)}
        return result
    result, actual = _eval_event(node, user)
    if explain:
        return {"op": op, "event": node["event"], "actual": actual,
                "expected": node.get("value"), "result": bool(result)}
    return result


def _attr_value(user: UserProfile, field: str) -> Any:
    if field.startswith("attr."):
        return user.attributes.get(field[len("attr."):])
    if field == "user_id":
        return user.user_id
    return None


def _eval_field(node: dict, user: UserProfile) -> bool:
    actual = _attr_value(user, node["field"])
    return _compare(actual, node["op"], node.get("value"))


def _compare(actual: Any, op: str, expected: Any) -> bool:
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "in":
            return actual in (expected or [])
        if op == "not_in":
            return actual not in (expected or [])
        if actual is None:
            return False
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
    except TypeError:
        return False
    return False


def _matching_events(node: dict, user: UserProfile) -> list[BehaviorEvent]:
    wanted = node["event"]
    since = node.get("since")
    min_amount = node.get("min_amount")
    out: list[BehaviorEvent] = []
    for ev in user.events:
        if wanted != "*" and ev.event_type != wanted:
            continue
        if since is not None and ev.occurred_at < since:
            continue
        if min_amount is not None and ev.amount < float(min_amount):
            continue
        out.append(ev)
    return out


def _eval_event(node: dict, user: UserProfile) -> tuple[bool, Any]:
    events = _matching_events(node, user)
    op = node["op"]
    value = node.get("value")
    if op in ("count",):
        return True, len(events)
    if op == "count_gte":
        return len(events) >= value, len(events)
    if op == "count_lte":
        return len(events) <= value, len(events)
    if op == "sum_amount":
        return True, round(sum(e.amount for e in events), 6)
    if op == "sum_amount_gte":
        total = round(sum(e.amount for e in events), 6)
        return total >= value, total
    if op == "ever":
        return len(events) > 0, len(events)
    if op == "never":
        return len(events) == 0, len(events)
    if op == "last_within_days":
        if not events:
            return False, None
        latest = max(e.occurred_at for e in events)
        cutoff_ts = (datetime.fromisoformat(_strip_z(latest)).timestamp()
                     - float(value) * 86400)
        cutoff = datetime.utcfromtimestamp(cutoff_ts).isoformat() + "Z"
        return latest >= cutoff, latest
    return False, None


def _strip_z(ts: str) -> str:
    return ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
