"""Campaign lifecycle management and scheduled activation.

Legal status graph::

    DRAFT     -> SCHEDULED | ENDED
    SCHEDULED -> ACTIVE (manual or auto at start_at) | ENDED
    ACTIVE    -> PAUSED | ENDED
    PAUSED    -> ACTIVE | ENDED
    ENDED     -> (terminal)

Every out-of-graph request raises :class:`InvalidTransitionError` with a
message containing both source and target status, and is recorded in the
audit log (including attempts rejected for authorization reasons).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .authz import Actor, Authorizer, normalize_ts, utcnow_iso
from .errors import InvalidTransitionError, NotFoundError
from .models import Campaign, CampaignStatus
from .storage import Storage

_ALLOWED: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset(
        {CampaignStatus.SCHEDULED, CampaignStatus.ENDED}),
    CampaignStatus.SCHEDULED: frozenset(
        {CampaignStatus.ACTIVE, CampaignStatus.ENDED}),
    CampaignStatus.ACTIVE: frozenset(
        {CampaignStatus.PAUSED, CampaignStatus.ENDED}),
    CampaignStatus.PAUSED: frozenset(
        {CampaignStatus.ACTIVE, CampaignStatus.ENDED}),
    CampaignStatus.ENDED: frozenset(),
}

_ACTION_BY_TARGET = {
    CampaignStatus.SCHEDULED: "campaign.publish",
    CampaignStatus.ACTIVE: "campaign.resume",
    CampaignStatus.PAUSED: "campaign.pause",
    CampaignStatus.ENDED: "campaign.end",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_canonical(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return normalize_ts(value.astimezone(timezone.utc).isoformat())
    return normalize_ts(str(value))


def _row_to_campaign(row) -> Campaign:
    return Campaign(
        id=row["id"], name=row["name"], status=CampaignStatus(row["status"]),
        total_quota=row["total_quota"], per_user_quota=row["per_user_quota"],
        issued_count=row["issued_count"], rule_version=row["rule_version"],
        start_at=parse_ts(row["start_at"]), end_at=parse_ts(row["end_at"]),
        created_at=parse_ts(row["created_at"]),
        updated_at=parse_ts(row["updated_at"]))


class CampaignService:
    def __init__(self, storage: Storage, authorizer: Authorizer | None = None):
        self.storage = storage
        self.authorizer = authorizer or Authorizer(storage)

    # ------------------------------------------------------------- CRUD-ish
    def create_campaign(self, name: str, total_quota: int, per_user_quota: int,
                        start_at: datetime | None = None,
                        end_at: datetime | None = None,
                        actor: Actor | None = None) -> Campaign:
        if total_quota <= 0 or per_user_quota <= 0:
            raise InvalidTransitionError("quotas must be positive integers",
                                         code="invalid_quota")
        if per_user_quota > total_quota:
            raise InvalidTransitionError(
                "per_user_quota cannot exceed total_quota",
                code="invalid_quota")
        if end_at is not None and start_at is not None \
                and _to_canonical(end_at) <= _to_canonical(start_at):
            raise InvalidTransitionError("end_at must be after start_at",
                                         code="invalid_quota")
        if actor is not None:
            self.authorizer.authorize(
                actor, "campaign.create", "campaign", 0, f"name={name}")
        initial = CampaignStatus.SCHEDULED if start_at else CampaignStatus.DRAFT
        ts = utcnow_iso()
        with self.storage.tx() as conn:
            cur = conn.execute(
                "INSERT INTO campaigns(name, status, total_quota,"
                " per_user_quota, issued_count, start_at, end_at,"
                " created_at, updated_at) VALUES (?,?,?,?,0,?,?,?,?)",
                (name, initial.value, total_quota, per_user_quota,
                 _to_canonical(start_at), _to_canonical(end_at), ts, ts))
            campaign_id = cur.lastrowid
        return self.get(campaign_id)

    def get(self, campaign_id: int) -> Campaign:
        row = self.storage.query_one(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        if row is None:
            raise NotFoundError(f"campaign {campaign_id} not found")
        return _row_to_campaign(row)

    # ---------------------------------------------------------- transitions
    def transition(self, campaign_id: int, target: str | CampaignStatus,
                   actor: Actor | None = None,
                   *, auto: bool = False) -> Campaign:
        target = CampaignStatus(target)
        campaign = self.get(campaign_id)
        if not auto and actor is not None and target in _ACTION_BY_TARGET:
            self.authorizer.authorize(
                actor, _ACTION_BY_TARGET[target], "campaign", campaign_id,
                f"{campaign.status.value} -> {target.value}")
        if target not in _ALLOWED[campaign.status]:
            raise InvalidTransitionError(
                f"illegal transition {campaign.status.value} -> {target.value}")
        ts = utcnow_iso()
        with self.storage.tx() as conn:
            # re-check status inside the write lock
            row = conn.execute(
                "SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            current = CampaignStatus(row["status"])
            if target not in _ALLOWED[current]:
                raise InvalidTransitionError(
                    f"illegal transition {current.value} -> {target.value}")
            conn.execute("UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
                         (target.value, ts, campaign_id))
        return self.get(campaign_id)

    # ------------------------------------------------- scheduled activation
    def activate_due(self, now: datetime | None = None) -> list[int]:
        """Activate SCHEDULED campaigns whose ``start_at`` has passed.

        Also ends ACTIVE campaigns whose ``end_at`` has passed.  Runs the
        scan plus updates in one IMMEDIATE transaction, so the result is
        idempotent and safe under restarts: an activated campaign never
        reverts to SCHEDULED.
        """
        now = now or now_utc()
        now_iso = normalize_ts(now.astimezone(timezone.utc).isoformat())
        activated: list[int] = []
        with self.storage.tx() as conn:
            rows = conn.execute(
                "SELECT id, status, start_at, end_at FROM campaigns"
                " WHERE status IN (?,?)",
                (CampaignStatus.SCHEDULED.value, CampaignStatus.ACTIVE.value),
            ).fetchall()
            for row in rows:
                status = CampaignStatus(row["status"])
                if (status is CampaignStatus.SCHEDULED
                        and row["start_at"] is not None
                        and row["start_at"] <= now_iso):
                    conn.execute(
                        "UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
                        (CampaignStatus.ACTIVE.value, utcnow_iso(), row["id"]))
                    activated.append(row["id"])
                elif (status is CampaignStatus.ACTIVE
                      and row["end_at"] is not None
                      and row["end_at"] <= now_iso):
                    conn.execute(
                        "UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
                        (CampaignStatus.ENDED.value, utcnow_iso(), row["id"]))
        return activated
