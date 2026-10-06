"""权限：越权拒绝且留审计痕迹。"""
import pytest

from app.campaigns import create_campaign
from app.db import tx
from app.errors import BizError, ErrorCode
from app.models import Actor, Role
from app.security import list_audit, require

OP = Actor("op1", frozenset({Role.OPERATOR}))
AGENT = Actor("agent1", frozenset({Role.AGENT}))
VIEWER = Actor("v1", frozenset({Role.VIEWER}))
ADMIN = Actor("boss", frozenset({Role.ADMIN}))


def test_operator_can_publish_pause_end(conn):
    for action in ("campaign.publish", "campaign.pause", "campaign.end"):
        require(conn, OP, action, "c1")
    logs = list_audit(conn)
    assert len(logs) == 3 and all(l["allowed"] == 1 for l in logs)


def test_agent_cannot_publish_but_can_redeem(conn):
    with pytest.raises(BizError) as ei:
        require(conn, AGENT, "campaign.publish", "c1")
    assert ei.value.code == ErrorCode.FORBIDDEN
    require(conn, AGENT, "voucher.redeem_for_user", "v1")


def test_viewer_denied_everything_and_audited(conn):
    for action in ("campaign.publish", "campaign.end", "voucher.issue",
                   "batch.issue", "voucher.redeem_for_user"):
        with pytest.raises(BizError):
            require(conn, VIEWER, action, "x")
    denied = [l for l in list_audit(conn) if l["allowed"] == 0]
    assert len(denied) == 5
    assert all(l["actor"] == "v1" and l["reason"] for l in denied)


def test_admin_has_all_roles(conn):
    for action in ("campaign.publish", "campaign.pause", "campaign.end",
                   "voucher.issue", "voucher.redeem_for_user", "batch.issue"):
        require(conn, ADMIN, action, "x")
