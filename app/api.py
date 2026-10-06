"""FastAPI application assembly."""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .audience import AudienceService
from .authz import Actor, Authorizer
from .batch import BatchService
from .errors import (AppError, NotFoundError, PermissionDeniedError,
                     RuleValidationError)
from .lifecycle import CampaignService
from .models import Role
from .referrals import ReferralService
from .rule_engine import BehaviorEvent, UserProfile
from .scheduler import ActivationScheduler
from .storage import Storage, open_storage
from .vouchers import VoucherService

_HTTP_STATUS = {
    NotFoundError: 404,
    PermissionDeniedError: 403,
    RuleValidationError: 400,
}


class Services:
    def __init__(self, storage: Storage, *, start_scheduler: bool = False):
        self.storage = storage
        self.authorizer = Authorizer(storage)
        self.campaigns = CampaignService(storage, self.authorizer)
        self.audience = AudienceService(storage, self.authorizer)
        self.vouchers = VoucherService(storage, self.authorizer, self.audience)
        self.referrals = ReferralService(storage, self.authorizer)
        self.batch = BatchService(storage, self.vouchers, self.authorizer)
        self.scheduler = ActivationScheduler(self.campaigns)
        if start_scheduler:
            self.scheduler.start()

    def shutdown(self) -> None:
        self.scheduler.stop()


def _actor(actor_id: str | None, actor_role: str | None) -> Actor:
    if not actor_id or not actor_role:
        # default subject: an end user; privileged actions are still denied
        return Actor(id=actor_id or "anonymous", role=Role.USER)
    try:
        return Actor.parse(actor_id, actor_role)
    except ValueError:
        raise PermissionDeniedError(f"unknown role {actor_role!r}")


def create_app(services: Services | None = None) -> FastAPI:
    svc = services or Services(open_storage(), start_scheduler=False)
    app = FastAPI(title="Campaign Audience Engine")
    app.state.services = svc

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError):
        status = 409
        for cls, code in _HTTP_STATUS.items():
            if isinstance(exc, cls):
                status = code
                break
        return JSONResponse(status_code=status,
                            content={"error": {"code": exc.code,
                                               "message": str(exc)}})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ------------------------------------------------------------ campaigns
    @app.post("/api/campaigns", status_code=201)
    def create_campaign(body: dict,
                        x_actor_id: str | None = Header(default=None),
                        x_actor_role: str | None = Header(default=None)):
        actor = _actor(x_actor_id, x_actor_role)
        c = svc.campaigns.create_campaign(
            name=body["name"], total_quota=int(body["total_quota"]),
            per_user_quota=int(body["per_user_quota"]),
            start_at=body.get("start_at"), end_at=body.get("end_at"),
            actor=actor)
        return _campaign_json(c)

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: int):
        return _campaign_json(svc.campaigns.get(campaign_id))

    @app.post("/api/campaigns/{campaign_id}/transition")
    def transition(campaign_id: int, body: dict,
                   x_actor_id: str | None = Header(default=None),
                   x_actor_role: str | None = Header(default=None)):
        actor = _actor(x_actor_id, x_actor_role)
        c = svc.campaigns.transition(campaign_id, body["target"], actor)
        return _campaign_json(c)

    # ----------------------------------------------------------------- rule
    @app.put("/api/campaigns/{campaign_id}/rule")
    def put_rule(campaign_id: int, body: dict,
                 x_actor_id: str | None = Header(default=None),
                 x_actor_role: str | None = Header(default=None)):
        actor = _actor(x_actor_id, x_actor_role)
        rule = svc.audience.upsert_rule(campaign_id, body["rule"], actor)
        return {"id": rule.id, "campaign_id": rule.campaign_id,
                "version": rule.version, "fingerprint": rule.fingerprint}

    @app.post("/api/campaigns/{campaign_id}/selection")
    def selection(campaign_id: int, body: dict):
        users = [_profile_from_dict(u) for u in body.get("users", [])]
        return svc.audience.run_selection(campaign_id, users)

    @app.post("/api/campaigns/{campaign_id}/explain")
    def explain(campaign_id: int, body: dict):
        user = _profile_from_dict(body["user"])
        version = body.get("version")
        return svc.audience.explain(campaign_id, version, user)

    # ------------------------------------------------------------- vouchers
    @app.post("/api/campaigns/{campaign_id}/vouchers", status_code=201)
    def issue(campaign_id: int, body: dict,
              x_actor_id: str | None = Header(default=None),
              x_actor_role: str | None = Header(default=None)):
        actor = _actor(x_actor_id, x_actor_role)
        profile = (_profile_from_dict(body["profile"])
                   if body.get("profile") else None)
        v = svc.vouchers.issue(campaign_id, body["user_id"],
                               body["request_id"], actor, profile=profile)
        return _voucher_json(v)

    @app.post("/api/vouchers/{voucher_id}/redeem")
    def redeem(voucher_id: int, body: dict,
               x_actor_id: str | None = Header(default=None),
               x_actor_role: str | None = Header(default=None)):
        actor = _actor(x_actor_id, x_actor_role)
        v = svc.vouchers.redeem(
            voucher_id, body["request_id"], actor,
            expected_user_id=body.get("user_id"))
        return _voucher_json(v)

    @app.get("/api/vouchers/{voucher_id}")
    def get_voucher(voucher_id: int):
        return _voucher_json(svc.vouchers.get(voucher_id))

    # ------------------------------------------------------------ referrals
    @app.post("/api/referrals", status_code=201)
    def register_referral(body: dict):
        return svc.referrals.register(body["referrer_id"],
                                      body["referred_id"],
                                      int(body["campaign_id"]))

    @app.post("/api/conversions")
    def conversion(body: dict):
        return svc.referrals.record_conversion(
            body["event_id"], body["user_id"], int(body["campaign_id"]),
            body["occurred_at"])

    @app.get("/api/attributions/{event_id}")
    def attribution(event_id: str):
        result = svc.referrals.attribution_for(event_id)
        if result is None:
            raise NotFoundError(f"event {event_id} not seen")
        return result

    # ---------------------------------------------------------------- batch
    @app.post("/api/batches", status_code=201)
    def submit_batch(body: dict,
                     x_actor_id: str | None = Header(default=None),
                     x_actor_role: str | None = Header(default=None)):
        actor = _actor(x_actor_id, x_actor_role)
        return svc.batch.submit(int(body["campaign_id"]),
                                body["idempotency_key"],
                                list(body["user_ids"]), actor)

    @app.post("/api/batches/{job_id}/run")
    def run_batch(job_id: int, body: dict | None = None):
        fail_after = (body or {}).get("fail_after")
        try:
            return svc.batch.run(job_id, fail_after)
        except RuntimeError as exc:
            # simulated crash hook exposed for recovery tests
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/batches/{job_id}/resume")
    def resume_batch(job_id: int):
        return svc.batch.resume(job_id)

    @app.get("/api/batches/{job_id}")
    def get_batch(job_id: int):
        return svc.batch.get(job_id)

    # ----------------------------------------------------------------- audit
    @app.get("/api/audit")
    def list_audit(target_type: str | None = None,
                   target_id: str | None = None,
                   allowed: bool | None = None):
        return {"entries": svc.authorizer.list_audit(
            target_type, target_id, allowed)}

    return app


def _campaign_json(c) -> dict:
    return {"id": c.id, "name": c.name, "status": c.status.value,
            "total_quota": c.total_quota,
            "per_user_quota": c.per_user_quota,
            "issued_count": c.issued_count,
            "rule_version": c.rule_version,
            "start_at": c.start_at.isoformat() if c.start_at else None,
            "end_at": c.end_at.isoformat() if c.end_at else None}


def _voucher_json(v) -> dict:
    return {"id": v.id, "campaign_id": v.campaign_id,
            "user_id": v.user_id, "status": v.status.value,
            "request_id": v.request_id, "rule_version": v.rule_version,
            "redeemed_by": v.redeemed_by}


def _profile_from_dict(data: dict) -> UserProfile:
    events = tuple(
        BehaviorEvent(event_type=e["event_type"],
                      occurred_at=e["occurred_at"],
                      amount=float(e.get("amount", 0.0)))
        for e in data.get("events", []))
    return UserProfile(user_id=data["user_id"],
                       attributes=dict(data.get("attributes", {})),
                       events=events)


# process-wide app (imported by main.py / uvicorn); CAMPAIGN_DB controls
# where durable state lives so restarts keep status and schedule. When
# CAMPAIGN_DB is set the background scheduler is enabled and performs a
# catch-up activation on boot.
import os as _os

_start_scheduler = _os.environ.get("CAMPAIGN_DB") is not None
app = create_app(Services(open_storage(), start_scheduler=_start_scheduler))
