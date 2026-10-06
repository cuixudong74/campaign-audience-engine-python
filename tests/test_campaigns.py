"""生命周期：合法/非法流转、预约自动激活、重启后状态不错乱。"""
import pytest

from app.campaigns import (activate_due_campaigns, create_campaign,
                           get_campaign, transition)
from app.db import connect
from app.errors import BizError, ErrorCode
from app.models import CampaignStatus


def test_legal_and_illegal_transitions(conn):
    c = create_campaign(conn, "p")
    assert c.status == CampaignStatus.DRAFT
    # 非法：draft -> paused / ended
    for bad in (CampaignStatus.PAUSED, CampaignStatus.ENDED):
        with pytest.raises(BizError) as ei:
            transition(conn, c.id, bad)
        assert ei.value.code == ErrorCode.INVALID_STATE_TRANSITION
        assert "draft" in ei.value.message and bad.value in ei.value.message
    # 合法路径：draft -> active -> paused -> active -> ended
    for target in (CampaignStatus.ACTIVE, CampaignStatus.PAUSED,
                   CampaignStatus.ACTIVE, CampaignStatus.ENDED):
        c = transition(conn, c.id, target)
        assert c.status == target
    # 终态不可再流转
    with pytest.raises(BizError) as ei:
        transition(conn, c.id, CampaignStatus.ACTIVE)
    assert ei.value.code == ErrorCode.INVALID_STATE_TRANSITION


def test_scheduled_auto_activation(conn):
    c = create_campaign(conn, "p", scheduled_at="2000-01-01T00:00:00+00:00")
    transition(conn, c.id, CampaignStatus.SCHEDULED)
    # 到点后读取即激活
    assert get_campaign(conn, c.id).status == CampaignStatus.ACTIVE


def test_scheduled_not_yet_due(conn):
    c = create_campaign(conn, "p", scheduled_at="2999-01-01T00:00:00+00:00")
    transition(conn, c.id, CampaignStatus.SCHEDULED)
    assert get_campaign(conn, c.id).status == CampaignStatus.SCHEDULED


def test_restart_keeps_state_and_schedule(db_path):
    conn = connect(db_path)
    c1 = create_campaign(conn, "active-one")
    transition(conn, c1.id, CampaignStatus.ACTIVE)
    c2 = create_campaign(conn, "future", scheduled_at="2999-01-01T00:00:00+00:00")
    transition(conn, c2.id, CampaignStatus.SCHEDULED)
    c3 = create_campaign(conn, "due", scheduled_at="2000-01-01T00:00:00+00:00")
    transition(conn, c3.id, CampaignStatus.SCHEDULED)
    conn.close()
    # 模拟重启：新连接，启动时校正
    conn2 = connect(db_path)
    activate_due_campaigns(conn2)
    assert get_campaign(conn2, c1.id).status == CampaignStatus.ACTIVE
    assert get_campaign(conn2, c2.id).status == CampaignStatus.SCHEDULED
    assert get_campaign(conn2, c3.id).status == CampaignStatus.ACTIVE
    conn2.close()
