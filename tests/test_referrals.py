"""推荐归因：乱序/迟到事件、幂等、自推与环拒绝。"""
import pytest

from app.errors import BizError, ErrorCode
from app.referrals import (list_attributions, record_conversion,
                           register_referral)

T = "2026-01-01T00:00:00+00:00"


def test_self_referral_rejected(conn):
    with pytest.raises(BizError) as ei:
        register_referral(conn, "a", "a")
    assert ei.value.code == ErrorCode.REFERRAL_SELF


def test_cycle_rejected(conn):
    register_referral(conn, "a", "b")
    register_referral(conn, "b", "c")
    with pytest.raises(BizError) as ei:
        register_referral(conn, "c", "a")
    assert ei.value.code == ErrorCode.REFERRAL_CYCLE


def test_event_before_relation_backfilled(conn):
    # 转化事件先到，推荐关系后登记（乱序）-> 关系建立时补归因
    record_conversion(conn, "e1", "buyer", T)
    register_referral(conn, "ref1", "buyer")
    atts = list_attributions(conn, "ref1")
    assert len(atts) == 1 and atts[0]["event_id"] == "e1"


def test_relation_before_event(conn):
    register_referral(conn, "ref1", "buyer")
    created, att = record_conversion(conn, "e1", "buyer", T)
    assert created and att == "e1"


def test_same_referral_attributed_once(conn):
    register_referral(conn, "ref1", "buyer")
    record_conversion(conn, "e1", "buyer", T)
    # 第二个转化事件：同一次推荐不再重复归因
    created, att = record_conversion(conn, "e2", "buyer", T)
    assert created and att == "e1"
    assert len(list_attributions(conn, "ref1")) == 1


def test_duplicate_event_idempotent(conn):
    register_referral(conn, "ref1", "buyer")
    record_conversion(conn, "e1", "buyer", T)
    created, att = record_conversion(conn, "e1", "buyer", T)  # 重复到达
    assert not created and att == "e1"
    assert len(list_attributions(conn, "ref1")) == 1


def test_referee_cannot_change_referrer(conn):
    register_referral(conn, "ref1", "buyer")
    created, _ = register_referral(conn, "ref1", "buyer")  # 同人重复登记幂等
    assert not created
    with pytest.raises(BizError) as ei:
        register_referral(conn, "ref2", "buyer")
    assert ei.value.code == ErrorCode.REFERRAL_DUPLICATE
