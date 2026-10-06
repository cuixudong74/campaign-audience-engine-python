import pytest

from app.authz import Actor
from app.errors import PermissionDeniedError
from app.models import Role


OPERATOR = Actor("op", Role.OPERATOR)
USER = Actor("u", Role.USER)
AGENT = Actor("ag", Role.AGENT)


def test_only_privileged_roles_can_publish(services):
    c = services.campaigns.create_campaign("c", 10, 2, actor=OPERATOR)
    with pytest.raises(PermissionDeniedError):
        services.campaigns.transition(c.id, "scheduled", USER)
    # operator succeeds along the legal path
    services.campaigns.transition(c.id, "scheduled", OPERATOR)
    services.campaigns.transition(c.id, "active", OPERATOR)


def test_pause_end_require_role(services):
    c = services.campaigns.create_campaign("c", 10, 2, actor=OPERATOR)
    services.campaigns.transition(c.id, "scheduled", OPERATOR)
    services.campaigns.transition(c.id, "active", OPERATOR)
    with pytest.raises(PermissionDeniedError):
        services.campaigns.transition(c.id, "paused", AGENT)
    services.campaigns.transition(c.id, "paused", OPERATOR)
    services.campaigns.transition(c.id, "ended", OPERATOR)


def test_denied_attempts_are_audited(services):
    c = services.campaigns.create_campaign("c", 10, 2, actor=OPERATOR)
    with pytest.raises(PermissionDeniedError):
        services.campaigns.transition(c.id, "scheduled", USER)
    logs = services.authorizer.list_audit(allowed=False)
    assert logs[-1]["actor_id"] == "u"
    assert logs[-1]["action"] == "campaign.publish"
    assert logs[-1]["allowed"] == 0


def test_allowed_attempts_are_audited(services):
    services.campaigns.create_campaign("c", 10, 2, actor=OPERATOR)
    ok = services.authorizer.list_audit(allowed=True)
    assert any(log["action"] == "campaign.create" for log in ok)


def test_agent_cannot_manage_campaign(services):
    c = services.campaigns.create_campaign("c", 10, 2, actor=OPERATOR)
    with pytest.raises(PermissionDeniedError):
        services.campaigns.transition(c.id, "scheduled", AGENT)
