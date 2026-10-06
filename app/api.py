"""FastAPI 路由层：把领域服务暴露为 HTTP API。

- 身份：X-Actor / X-Roles 头解析为 Actor；
- 每个请求独立连接（WAL 下读写并发安全），请求结束关闭；
- BizError 统一映射为 {code, message}，错误原因可区分。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import audience, batch, campaigns, referrals, security, vouchers
from .db import connect
from .errors import BizError, ErrorCode
from .models import Actor, CampaignStatus, Role


# ---- 请求模型 ----

class CreateCampaignReq(BaseModel):
    name: str
    scheduled_at: Optional[str] = None
    total_quota: Optional[int] = None
    per_user_limit: int = 1
    audience_rule_id: Optional[str] = None
    audience_rule_version: Optional[int] = None


class TransitionReq(BaseModel):
    target: CampaignStatus


class RuleReq(BaseModel):
    name: str
    definition: dict


class RuleUpdateReq(BaseModel):
    definition: dict


class SelectReq(BaseModel):
    rule_id: str
    version: Optional[int] = None
    users: dict[str, dict]


class ExplainReq(BaseModel):
    rule_id: str
    version: Optional[int] = None
    user: dict


class IssueReq(BaseModel):
    user_id: str
    idempotency_key: str
    user_ctx: Optional[dict] = None   # 提供时按绑定规则校验人群资格


class RedeemReq(BaseModel):
    idempotency_key: str


class ReferralReq(BaseModel):
    referrer_id: str
    referee_id: str


class ConversionReq(BaseModel):
    event_id: str
    user_id: str
    occurred_at: str


class BatchReq(BaseModel):
    user_ids: list[str]


# ---- 依赖 ----

def get_conn(request: Request):
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_actor(x_actor: Optional[str] = Header(None),
              x_roles: str = Header("")) -> Actor:
    if not x_actor:
        raise BizError(ErrorCode.UNAUTHENTICATED, "missing X-Actor header", 401)
    roles = {Role(r.strip()) for r in x_roles.split(",") if r.strip()}
    return Actor(subject=x_actor, roles=frozenset(roles))


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="campaign-audience-engine")
    app.state.db_path = db_path

    @app.exception_handler(BizError)
    async def biz_error_handler(_req, exc: BizError):
        return JSONResponse(status_code=exc.http_status,
                            content={"code": exc.code, "message": exc.message})

    @app.exception_handler(ValueError)
    async def value_error_handler(_req, exc: ValueError):
        return JSONResponse(status_code=400,
                            content={"code": ErrorCode.VALIDATION, "message": str(exc)})

    @app.on_event("startup")
    def _activate_due():
        conn = connect(db_path)
        try:
            campaigns.activate_due_campaigns(conn)  # 重启后按预约时间校正状态
        finally:
            conn.close()

    # ---- 活动生命周期 ----

    @app.post("/campaigns", status_code=201)
    def create_campaign(req: CreateCampaignReq, conn=Depends(get_conn),
                        actor: Actor = Depends(get_actor)):
        security.require(conn, actor, "campaign.publish", "campaign:new")
        c = campaigns.create_campaign(conn, req.name, req.scheduled_at,
                                      req.total_quota, req.per_user_limit,
                                      req.audience_rule_id, req.audience_rule_version)
        return c.__dict__

    @app.get("/campaigns/{cid}")
    def get_campaign(cid: str, conn=Depends(get_conn)):
        return campaigns.get_campaign(conn, cid).__dict__

    @app.post("/campaigns/{cid}/transitions")
    def transition(cid: str, req: TransitionReq, conn=Depends(get_conn),
                   actor: Actor = Depends(get_actor)):
        action = {CampaignStatus.ACTIVE: "campaign.publish",
                  CampaignStatus.SCHEDULED: "campaign.publish",
                  CampaignStatus.PAUSED: "campaign.pause",
                  CampaignStatus.ENDED: "campaign.end"}[req.target]
        security.require(conn, actor, action, f"campaign:{cid}")
        return campaigns.transition(conn, cid, req.target).__dict__

    # ---- 人群规则 ----

    @app.post("/rules", status_code=201)
    def create_rule(req: RuleReq, conn=Depends(get_conn)):
        rid, version = audience.create_rule(conn, req.name, req.definition)
        return {"id": rid, "version": version}

    @app.put("/rules/{rid}")
    def update_rule(rid: str, req: RuleUpdateReq, conn=Depends(get_conn)):
        return {"id": rid, "version": audience.update_rule(conn, rid, req.definition)}

    @app.get("/rules/{rid}")
    def get_rule(rid: str, version: Optional[int] = None, conn=Depends(get_conn)):
        v, definition = audience.get_rule(conn, rid, version)
        return {"id": rid, "version": v, "definition": definition}

    @app.post("/audience/select")
    def select(req: SelectReq, conn=Depends(get_conn)):
        _, definition = audience.get_rule(conn, req.rule_id, req.version)
        return {"user_ids": audience.select(definition, req.users)}

    @app.post("/audience/explain")
    def explain(req: ExplainReq, conn=Depends(get_conn)):
        v, definition = audience.get_rule(conn, req.rule_id, req.version)
        result = audience.explain(definition, req.user)
        result["rule_version"] = v
        return result

    # ---- 凭证 ----

    @app.post("/campaigns/{cid}/vouchers", status_code=201)
    def issue(cid: str, req: IssueReq, conn=Depends(get_conn),
              actor: Actor = Depends(get_actor)):
        security.require(conn, actor, "voucher.issue", f"campaign:{cid}")
        c = campaigns.get_campaign(conn, cid)
        if c.audience_rule_id and req.user_ctx is not None:
            _, definition = audience.get_rule(conn, c.audience_rule_id)
            if not audience.evaluate(definition, req.user_ctx)[0]:
                raise BizError(ErrorCode.USER_NOT_IN_AUDIENCE,
                               f"user {req.user_id} not in audience", 409)
        v, created = vouchers.issue_safe(conn, cid, req.user_id, req.idempotency_key)
        return {"voucher": v.__dict__, "created": created}

    @app.post("/vouchers/{vid}/redeem")
    def redeem(vid: str, req: RedeemReq, conn=Depends(get_conn),
               actor: Actor = Depends(get_actor)):
        security.require(conn, actor, "voucher.redeem_for_user", f"voucher:{vid}")
        v, now = vouchers.redeem(conn, vid, req.idempotency_key)
        return {"voucher": v.__dict__, "redeemed_now": now}

    @app.get("/vouchers/{vid}")
    def get_voucher(vid: str, conn=Depends(get_conn)):
        return vouchers.get_voucher(conn, vid).__dict__

    # ---- 推荐归因 ----

    @app.post("/referrals", status_code=201)
    def register_referral(req: ReferralReq, conn=Depends(get_conn)):
        created, referrer = referrals.register_referral(
            conn, req.referrer_id, req.referee_id)
        return {"created": created, "referrer_id": referrer}

    @app.post("/conversions", status_code=201)
    def record_conversion(req: ConversionReq, conn=Depends(get_conn)):
        created, att = referrals.record_conversion(
            conn, req.event_id, req.user_id, req.occurred_at)
        return {"event_created": created, "attributed_event_id": att}

    @app.get("/referrals/{referrer_id}/attributions")
    def list_attributions(referrer_id: str, conn=Depends(get_conn)):
        return {"attributions": referrals.list_attributions(conn, referrer_id)}

    # ---- 批量发放 ----

    @app.post("/campaigns/{cid}/batches", status_code=201)
    def create_and_run_batch(cid: str, req: BatchReq, conn=Depends(get_conn),
                             actor: Actor = Depends(get_actor)):
        security.require(conn, actor, "batch.issue", f"campaign:{cid}")
        bid = batch.create_batch(conn, cid, req.user_ids)
        return batch.run_batch(conn, bid)

    @app.post("/batches/{bid}/resume")
    def resume_batch(bid: str, conn=Depends(get_conn),
                     actor: Actor = Depends(get_actor)):
        security.require(conn, actor, "batch.issue", f"batch:{bid}")
        return batch.resume_batch(conn, bid)

    @app.get("/batches/{bid}")
    def get_batch(bid: str, conn=Depends(get_conn)):
        return batch.get_batch(conn, bid)

    # ---- 审计 ----

    @app.get("/audit")
    def list_audit(target: Optional[str] = None, conn=Depends(get_conn)):
        return {"audit": security.list_audit(conn, target)}

    return app
