"""Voucher issuance / redemption with idempotency and quota guarantees.

All decisions happen inside a single ``BEGIN IMMEDIATE`` transaction:

* ``request_id`` has a UNIQUE constraint, so submitting the same issuance
  request twice returns the original voucher instead of creating a second
  one (idempotency, including across processes);
* ``redeem_request_id`` is UNIQUE and the voucher row is conditionally
  updated ``WHERE status='issued'``, so a voucher can never be redeemed
  twice and duplicate redeem requests are detected;
* total/per-user counters are read and incremented under the same writer
  lock, which bounds the campaign quota without overselling.
"""
from __future__ import annotations

import sqlite3

from .authz import Actor, Authorizer
from .errors import (ConflictError, InvalidTransitionError, NotFoundError,
                     PerUserLimitError, PermissionDeniedError,
                     QuotaExhaustedError, VoucherNotRedeemableError)
from .lifecycle import utcnow_iso
from .models import CampaignStatus, Role, Voucher, VoucherStatus
from .rule_engine import evaluate as _evaluate_rule
from .storage import Storage


def _row_to_voucher(row) -> Voucher:
    from .lifecycle import parse_ts
    return Voucher(
        id=row["id"], campaign_id=row["campaign_id"], user_id=row["user_id"],
        status=VoucherStatus(row["status"]), request_id=row["request_id"],
        rule_version=row["rule_version"],
        issued_at=parse_ts(row["issued_at"]),
        redeemed_at=parse_ts(row["redeemed_at"]),
        redeemed_by=row["redeemed_by"],
        redeem_request_id=row["redeem_request_id"])


class _OwnerMismatch(Exception):
    def __init__(self, owner_id: str):
        self.owner_id = owner_id


class VoucherService:
    def __init__(self, storage: Storage, authorizer: Authorizer | None = None,
                 audience=None):
        self.storage = storage
        self.authorizer = authorizer or Authorizer(storage)
        self.audience = audience

    # ---------------------------------------------------------------- issue
    def issue(self, campaign_id: int, user_id: str, request_id: str,
              actor: Actor | None = None,
              *, check_eligibility=None,
              profile=None) -> Voucher:
        """Issue one voucher.

        When the campaign has an audience rule, callers may pass the
        user's :class:`UserProfile` (``profile``) to enforce targeting; a
        non-matching user is rejected with ``not_in_audience``. An
        explicit ``check_eligibility`` callable overrides the default.
        """
        if actor is not None:
            self.authorizer.authorize(
                actor, "voucher.issue", "campaign", campaign_id,
                f"user={user_id}")
        try:
            with self.storage.tx() as conn:
                # idempotent replay?
                prev = conn.execute(
                    "SELECT * FROM vouchers WHERE request_id=?",
                    (request_id,)).fetchone()
                if prev is not None:
                    if (prev["campaign_id"] != campaign_id
                            or prev["user_id"] != user_id):
                        raise ConflictError(
                            "request_id already used with different payload")
                    return _row_to_voucher(prev)

                camp = conn.execute(
                    "SELECT * FROM campaigns WHERE id=?",
                    (campaign_id,)).fetchone()
                if camp is None:
                    raise NotFoundError(
                        f"campaign {campaign_id} not found")
                status = CampaignStatus(camp["status"])
                if status is not CampaignStatus.ACTIVE:
                    raise InvalidTransitionError(
                        f"campaign is {status.value}; only active campaigns"
                        " accept issuance", code="campaign_not_active")
                if camp["issued_count"] >= camp["total_quota"]:
                    raise QuotaExhaustedError(
                        f"campaign {campaign_id} total quota exhausted")
                used = conn.execute(
                    "SELECT COUNT(*) AS n FROM vouchers"
                    " WHERE campaign_id=? AND user_id=?",
                    (campaign_id, user_id)).fetchone()["n"]
                if used >= camp["per_user_quota"]:
                    raise PerUserLimitError(
                        f"user {user_id} reached per-user quota of"
                        f" {camp['per_user_quota']}")
                if check_eligibility is not None:
                    if not check_eligibility(user_id, camp["rule_version"]):
                        raise InvalidTransitionError(
                            "user not in current audience",
                            code="not_in_audience")
                elif (camp["rule_version"] is not None
                      and self.audience is not None and profile is not None):
                    rule = self.audience.get_rule(
                        campaign_id, camp["rule_version"])
                    if not _evaluate_rule(rule.body, profile):
                        raise InvalidTransitionError(
                            f"user {user_id} does not match audience rule"
                            f" v{rule.version}", code="not_in_audience")
                ts = utcnow_iso()
                cur = conn.execute(
                    "INSERT INTO vouchers(campaign_id, user_id, status,"
                    " request_id, rule_version, issued_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (campaign_id, user_id, VoucherStatus.ISSUED.value,
                     request_id, camp["rule_version"], ts))
                conn.execute(
                    "UPDATE campaigns SET issued_count=issued_count+1,"
                    " updated_at=? WHERE id=?", (ts, campaign_id))
                row = conn.execute("SELECT * FROM vouchers WHERE id=?",
                                   (cur.lastrowid,)).fetchone()
                return _row_to_voucher(row)
        except sqlite3.IntegrityError as exc:
            # concurrent insert with the same request_id -> replay lookup
            prev = self.storage.query_one(
                "SELECT * FROM vouchers WHERE request_id=?", (request_id,))
            if prev is not None:
                return _row_to_voucher(prev)
            raise ConflictError(f"integrity error: {exc}") from exc

    # --------------------------------------------------------------- redeem
    def redeem(self, voucher_id: int, request_id: str,
               actor: Actor | None = None,
               expected_user_id: str | None = None) -> Voucher:
        """Redeem a voucher idempotently.

        A USER-role actor may only redeem their own voucher (pass
        ``expected_user_id``).  An AGENT/ADMIN performs代客核销 and needs
        the ``voucher.agent_redeem`` permission.
        """
        as_agent = actor is not None and actor.role in (Role.AGENT, Role.ADMIN)
        if actor is not None:
            action = "voucher.agent_redeem" if as_agent else "voucher.self_redeem"
            self.authorizer.authorize(actor, action, "voucher", voucher_id)
        try:
            with self.storage.tx() as conn:
                prev = conn.execute(
                    "SELECT * FROM vouchers WHERE redeem_request_id=?",
                    (request_id,)).fetchone()
                if prev is not None:
                    if prev["id"] != voucher_id:
                        raise ConflictError(
                            "redeem request_id already used for another voucher")
                    if not as_agent and expected_user_id is not None \
                            and prev["user_id"] != expected_user_id:
                        raise _OwnerMismatch(prev["user_id"])
                    return _row_to_voucher(prev)

                row = conn.execute("SELECT * FROM vouchers WHERE id=?",
                                   (voucher_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"voucher {voucher_id} not found")
                if not as_agent and expected_user_id is not None \
                        and row["user_id"] != expected_user_id:
                    # confirmed cross-user attempt; abort the transaction and
                    # produce an audited permission denial outside it
                    raise _OwnerMismatch(row["user_id"])
                if row["status"] != VoucherStatus.ISSUED.value:
                    raise VoucherNotRedeemableError(
                        f"voucher {voucher_id} already {row['status']}")
                ts = utcnow_iso()
                actor_id = actor.id if actor else expected_user_id
                cur = conn.execute(
                    "UPDATE vouchers SET status=?, redeemed_at=?,"
                    " redeemed_by=?, redeem_request_id=?"
                    " WHERE id=? AND status=?",
                    (VoucherStatus.REDEEMED.value, ts, actor_id, request_id,
                     voucher_id, VoucherStatus.ISSUED.value))
                if cur.rowcount == 0:
                    raise VoucherNotRedeemableError(
                        f"voucher {voucher_id} was redeemed concurrently")
                return self.get(voucher_id)
        except _OwnerMismatch as exc:
            # durable audit *after* the business transaction has rolled back
            self.authorizer._log(
                actor if actor is not None
                else Actor(expected_user_id, Role.USER),
                "voucher.agent_redeem", "voucher", voucher_id, False,
                f"owner mismatch: voucher owned by {exc.owner_id}")
            raise PermissionDeniedError("voucher belongs to another user")
        except sqlite3.IntegrityError as e2:
            prev = self.storage.query_one(
                "SELECT * FROM vouchers WHERE redeem_request_id=?",
                (request_id,))
            if prev is not None:
                return _row_to_voucher(prev)
            raise ConflictError(f"integrity error: {e2}") from e2

    def get(self, voucher_id: int) -> Voucher:
        row = self.storage.query_one("SELECT * FROM vouchers WHERE id=?",
                                     (voucher_id,))
        if row is None:
            raise NotFoundError(f"voucher {voucher_id} not found")
        return _row_to_voucher(row)

    def count_for_user(self, campaign_id: int, user_id: str) -> int:
        return self.storage.query_one(
            "SELECT COUNT(*) AS n FROM vouchers WHERE campaign_id=? AND user_id=?",
            (campaign_id, user_id))["n"]
