from datetime import datetime, timedelta, timezone

import pytest

from app.authz import Actor
from app.errors import InvalidTransitionError, PermissionDeniedError
from app.models import CampaignStatus, Role


def test_legal_and_illegal_transitions(services):
    c = services.campaigns.create_campaign("c", 10, 2)
    assert c.status is CampaignStatus.DRAFT
    services.campaigns.transition(c.id, "scheduled")
    services.campaigns.transition(c.id, "active")
    services.campaigns.transition(c.id, "paused")
    services.campaigns.transition(c.id, "active")
    services.campaigns.transition(c.id, "ended")
    with pytest.raises(InvalidTransitionError) as ei:
        services.campaigns.transition(c.id, "active")
    assert "ended -> active" in str(ei.value)


def test_draft_cannot_activate_directly(services):
    c = services.campaigns.create_campaign("c", 10, 2)
    with pytest.raises(InvalidTransitionError) as ei:
        services.campaigns.transition(c.id, "active")
    assert ei.value.code == "invalid_transition"


def test_direct_active_from_paused_to_ended(services):
    c = services.campaigns.create_campaign("c", 10, 2)
    services.campaigns.transition(c.id, "scheduled")
    services.campaigns.transition(c.id, "active")
    services.campaigns.transition(c.id, "paused")
    assert services.campaigns.transition(c.id, "ended").status is CampaignStatus.ENDED


def test_scheduled_auto_activation(services):
    start = datetime.now(timezone.utc) + timedelta(seconds=30)
    c = services.campaigns.create_campaign("c", 10, 2, start_at=start)
    assert c.status is CampaignStatus.SCHEDULED
    # not due yet
    assert services.campaigns.activate_due() == []
    # due
    activated = services.campaigns.activate_due(start + timedelta(seconds=1))
    assert activated == [c.id]
    # idempotent
    assert services.campaigns.activate_due(start + timedelta(seconds=10)) == []
    assert services.campaigns.get(c.id).status is CampaignStatus.ACTIVE


def test_auto_end(services):
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    c = services.campaigns.create_campaign("c", 10, 2, start_at=start, end_at=end)
    services.campaigns.activate_due()
    # both due at once: scheduled -> active then? single pass ends via
    # SCHEDULED rows only activating; simulate second tick
    assert services.campaigns.get(c.id).status is CampaignStatus.ACTIVE
    services.campaigns.activate_due()
    assert services.campaigns.get(c.id).status is CampaignStatus.ENDED


def test_invalid_quota_rejected(services):
    with pytest.raises(InvalidTransitionError):
        services.campaigns.create_campaign("c", 0, 0)
    with pytest.raises(InvalidTransitionError):
        services.campaigns.create_campaign("c", 5, 6)


def test_permission_denied_transition_is_audited(services):
    c = services.campaigns.create_campaign("c", 10, 2)
    user = Actor("u1", Role.USER)
    with pytest.raises(PermissionDeniedError):
        services.campaigns.transition(c.id, "scheduled", user)
    logs = services.authorizer.list_audit(
        target_type="campaign", allowed=False)
    assert any(log["action"] == "campaign.publish" for log in logs)
