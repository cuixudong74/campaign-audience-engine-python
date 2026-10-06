"""统一错误码与异常定义。所有业务拒绝都带可区分的 reason code。"""
from __future__ import annotations


class ErrorCode:
    # 活动生命周期
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    CAMPAIGN_NOT_FOUND = "CAMPAIGN_NOT_FOUND"
    CAMPAIGN_NOT_ACTIVE = "CAMPAIGN_NOT_ACTIVE"
    # 人群规则
    RULE_NOT_FOUND = "RULE_NOT_FOUND"
    RULE_INVALID = "RULE_INVALID"
    # 凭证
    VOUCHER_NOT_FOUND = "VOUCHER_NOT_FOUND"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    PER_USER_LIMIT_REACHED = "PER_USER_LIMIT_REACHED"
    ALREADY_REDEEMED = "ALREADY_REDEEMED"
    USER_NOT_IN_AUDIENCE = "USER_NOT_IN_AUDIENCE"
    # 推荐归因
    REFERRAL_SELF = "REFERRAL_SELF"
    REFERRAL_CYCLE = "REFERRAL_CYCLE"
    REFERRAL_DUPLICATE = "REFERRAL_DUPLICATE"
    # 权限
    FORBIDDEN = "FORBIDDEN"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    # 通用
    VALIDATION = "VALIDATION"
    CONFLICT = "CONFLICT"


class BizError(Exception):
    """业务异常：code 可区分原因，http_status 供 API 层映射。"""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
