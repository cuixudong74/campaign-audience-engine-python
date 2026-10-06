"""Role based authorization and audit trail.

Every check (allowed or denied) is persisted to ``audit_logs`` so that
attempted越权操作 remain traceable.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from .errors import PermissionDeniedError
from .models import Role
from .storage import Storage


@dataclasses.dataclass(frozen=True)
class Actor:
    id: str
    role: Role

    @classmethod
    def parse(cls, actor_id: str, role: str) -> "Actor":
        return cls(actor_id, Role(role))


# action -> roles allowed to perform it
PERMISSIONS: dict[str, frozenset[Role]] = {
    "campaign.create": frozenset({Role.ADMIN, Role.OPERATOR}),
    "campaign.publish": frozenset({Role.ADMIN, Role.OPERATOR}),
    "campaign.pause": frozenset({Role.ADMIN, Role.OPERATOR}),
    "campaign.resume": frozenset({Role.ADMIN, Role.OPERATOR}),
    "campaign.end": frozenset({Role.ADMIN, Role.OPERATOR}),
    "rule.update": frozenset({Role.ADMIN, Role.OPERATOR}),
    "voucher.issue": frozenset(
        {Role.USER, Role.AGENT, Role.OPERATOR, Role.ADMIN}),
    "voucher.self_redeem": frozenset({Role.USER, Role.OPERATOR, Role.ADMIN}),
    "voucher.agent_redeem": frozenset({Role.AGENT, Role.ADMIN}),
    "batch.run": frozenset({Role.OPERATOR, Role.ADMIN}),
    "referral.register": frozenset({Role.USER, Role.OPERATOR, Role.ADMIN}),
}


def utcnow_iso() -> str:
    # canonical second-precision UTC, so lexical comparison of stored
    # timestamps matches chronological order (also for second-precision inputs)
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ts(value: str) -> str:
    """Normalise any ISO-8601 timestamp to canonical second-precision UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Authorizer:
    def __init__(self, storage: Storage):
        self.storage = storage

    def authorize(self, actor: Actor, action: str, target_type: str,
                  target_id: str | int, detail: str = "") -> None:
        allowed_roles = PERMISSIONS.get(action)
        allowed = allowed_roles is not None and actor.role in allowed_roles
        self._log(actor, action, target_type, str(target_id), allowed, detail)
        if not allowed:
            raise PermissionDeniedError(
                f"actor {actor.id!r} with role {actor.role.value!r} "
                f"may not perform {action!r}")

    def _log(self, actor: Actor, action: str, target_type: str,
             target_id: str, allowed: bool, detail: str) -> None:
        with self.storage.tx() as conn:
            conn.execute(
                "INSERT INTO audit_logs(actor_id, role, action, target_type,"
                " target_id, allowed, detail, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (actor.id, actor.role.value, action, target_type, target_id,
                 1 if allowed else 0, detail, utcnow_iso()))

    def list_audit(self, target_type: str | None = None,
                   target_id: str | None = None,
                   allowed: bool | None = None) -> list[dict]:
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        params: list = []
        if target_type is not None:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        if allowed is not None:
            sql += " AND allowed = ?"
            params.append(1 if allowed else 0)
        sql += " ORDER BY id"
        return [dict(r) for r in self.storage.query(sql, tuple(params))]
