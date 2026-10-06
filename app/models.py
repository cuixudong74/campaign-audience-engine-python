"""Domain enums and value/dataclass models (framework independent)."""
from __future__ import annotations

import dataclasses
import enum
from datetime import datetime


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class VoucherStatus(str, enum.Enum):
    ISSUED = "issued"
    REDEEMED = "redeemed"
    REVOKED = "revoked"


class Role(str, enum.Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    AGENT = "agent"
    USER = "user"


@dataclasses.dataclass
class Rule:
    id: int | None
    campaign_id: int
    version: int
    body: dict
    fingerprint: str
    created_at: datetime


@dataclasses.dataclass
class Campaign:
    id: int | None
    name: str
    status: CampaignStatus
    total_quota: int
    per_user_quota: int
    issued_count: int
    rule_version: int | None
    start_at: datetime | None
    end_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass
class Voucher:
    id: int | None
    campaign_id: int
    user_id: str
    status: VoucherStatus
    request_id: str
    rule_version: int | None
    issued_at: datetime
    redeemed_at: datetime | None
    redeemed_by: str | None
    redeem_request_id: str | None


@dataclasses.dataclass
class BatchJob:
    id: int | None
    campaign_id: int
    idempotency_key: str
    status: str  # pending | running | completed | failed
    next_offset: int
    total: int
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass
class Referral:
    id: int | None
    referrer_id: str
    referred_id: str
    campaign_id: int
    created_at: datetime


@dataclasses.dataclass
class Attribution:
    id: int | None
    referral_id: int
    event_id: str
    attributed: bool
    reason: str | None
    created_at: datetime


@dataclasses.dataclass
class AuditLog:
    id: int | None
    actor_id: str
    role: str
    action: str
    target_type: str
    target_id: str
    allowed: bool
    detail: str
    created_at: datetime
