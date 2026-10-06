"""Domain error hierarchy.

Each business error carries a stable ``code`` so callers can distinguish
failure reasons programmatically.
"""
from __future__ import annotations


class AppError(Exception):
    code: str = "app_error"

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message or self.__class__.__name__)
        if code is not None:
            self.code = code


class NotFoundError(AppError):
    code = "not_found"


class ConflictError(AppError):
    code = "conflict"


class InvalidTransitionError(AppError):
    code = "invalid_transition"


class RuleValidationError(AppError):
    code = "rule_validation_error"


class QuotaExhaustedError(AppError):
    code = "quota_exhausted"


class PerUserLimitError(AppError):
    code = "per_user_limit_reached"


class VoucherNotRedeemableError(AppError):
    code = "voucher_not_redeemable"


class DuplicateRequestError(AppError):
    code = "duplicate_request"


class ReferralRejectedError(AppError):
    code = "referral_rejected"


class PermissionDeniedError(AppError):
    code = "permission_denied"


class BatchNotResumableError(AppError):
    code = "batch_not_resumable"
