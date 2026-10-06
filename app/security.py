"""角色鉴权与审计。

- require() 校验主体角色，越权抛 BizError(FORBIDDEN)；
- 无论放行还是拒绝，都写审计日志（actor/action/target/allowed/reason），
  审计与业务操作在同一事务里提交，可追溯且不丢。
"""
from __future__ import annotations

import sqlite3

from .campaigns import now_iso
from .errors import BizError, ErrorCode
from .models import ACTION_ROLES, Actor, Role


def check(actor: Actor, action: str) -> tuple[bool, str]:
    allowed_roles = ACTION_ROLES.get(action)
    if allowed_roles is None:
        return False, f"unknown action {action}"
    if actor.roles & allowed_roles:
        return True, "ok"
    return False, f"requires one of {sorted(r.value for r in allowed_roles)}"


def audit(conn: sqlite3.Connection, actor: Actor, action: str,
          target: str, allowed: bool, reason: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, action, target, allowed, reason, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (actor.subject, action, target, 1 if allowed else 0, reason, now_iso()))


def require(conn: sqlite3.Connection, actor: Actor, action: str, target: str) -> None:
    """鉴权入口：放行或拒绝都会留下审计记录。

    必须在业务事务之外调用（连接处于自动提交模式），
    这样拒绝记录不会因后续业务回滚而丢失。
    """
    allowed, reason = check(actor, action)
    audit(conn, actor, action, target, allowed, reason)
    if not allowed:
        raise BizError(ErrorCode.FORBIDDEN, f"{actor.subject}: {reason}", 403)


def list_audit(conn: sqlite3.Connection, target: str | None = None) -> list[dict]:
    if target is None:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE target=? ORDER BY id", (target,)).fetchall()
    return [dict(r) for r in rows]
