"""核心数据模型与枚举。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


# 合法状态流转表：source -> {target}
ALLOWED_TRANSITIONS: dict[CampaignStatus, set[CampaignStatus]] = {
    CampaignStatus.DRAFT: {CampaignStatus.SCHEDULED, CampaignStatus.ACTIVE},
    CampaignStatus.SCHEDULED: {CampaignStatus.ACTIVE, CampaignStatus.ENDED},
    CampaignStatus.ACTIVE: {CampaignStatus.PAUSED, CampaignStatus.ENDED},
    CampaignStatus.PAUSED: {CampaignStatus.ACTIVE, CampaignStatus.ENDED},
    CampaignStatus.ENDED: set(),
}


class VoucherStatus(str, enum.Enum):
    ISSUED = "issued"
    REDEEMED = "redeemed"


class Role(str, enum.Enum):
    ADMIN = "admin"          # 全部权限
    OPERATOR = "operator"    # 发布/暂停/结束活动、批量发放
    AGENT = "agent"          # 代客核销
    VIEWER = "viewer"        # 只读


# 动作 -> 允许的角色
ACTION_ROLES: dict[str, set[Role]] = {
    "campaign.publish": {Role.ADMIN, Role.OPERATOR},
    "campaign.pause": {Role.ADMIN, Role.OPERATOR},
    "campaign.end": {Role.ADMIN, Role.OPERATOR},
    "voucher.issue": {Role.ADMIN, Role.OPERATOR},
    "voucher.redeem_for_user": {Role.ADMIN, Role.AGENT},
    "batch.issue": {Role.ADMIN, Role.OPERATOR},
}


@dataclass(frozen=True)
class Actor:
    """操作主体。"""
    subject: str
    roles: frozenset[Role]


@dataclass
class Campaign:
    id: str
    name: str
    status: CampaignStatus
    scheduled_at: Optional[str] = None      # ISO8601，预约开始时间
    total_quota: Optional[int] = None       # 总量限制，None 表示不限
    per_user_limit: int = 1                 # 每人限领
    audience_rule_id: Optional[str] = None  # 绑定的人群规则版本
    version: int = 0                        # 乐观锁版本
    created_at: str = ""
    updated_at: str = ""


@dataclass
class AudienceRule:
    id: str
    name: str
    version: int
    definition: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class Voucher:
    id: str
    campaign_id: str
    user_id: str
    status: VoucherStatus
    idempotency_key: str
    rule_version: Optional[int] = None      # 发放时使用的规则版本（可解释）
    issued_at: str = ""
    redeemed_at: Optional[str] = None
    redeem_idempotency_key: Optional[str] = None
