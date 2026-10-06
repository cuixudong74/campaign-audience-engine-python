"""凭证发放与核销。

一致性保证：
- 幂等：idempotency_key 有唯一索引，重复请求返回首次结果，不产生第二条记录；
- 不超发：额度扣减是单条条件 UPDATE（issued_count < total_quota），
  在 IMMEDIATE 事务内与凭证插入同生共死，并发下总额精确；
- 限领：同事务内统计该用户已发数量，超过 per_user_limit 即拒绝；
- 核销：状态条件 UPDATE（status='issued'），同一凭证不可能核销两次。
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Optional

from .campaigns import _apply_due_activation, now_iso
from .errors import BizError, ErrorCode
from .models import CampaignStatus, Voucher, VoucherStatus


def _row_to_voucher(r: sqlite3.Row) -> Voucher:
    return Voucher(
        id=r["id"], campaign_id=r["campaign_id"], user_id=r["user_id"],
        status=VoucherStatus(r["status"]), idempotency_key=r["idempotency_key"],
        rule_version=r["rule_version"], issued_at=r["issued_at"],
        redeemed_at=r["redeemed_at"], redeem_idempotency_key=r["redeem_idempotency_key"],
    )


def get_voucher(conn: sqlite3.Connection, voucher_id: str) -> Voucher:
    row = conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
    if row is None:
        raise BizError(ErrorCode.VOUCHER_NOT_FOUND, f"voucher {voucher_id} not found", 404)
    return _row_to_voucher(row)


def issue(
    conn: sqlite3.Connection,
    campaign_id: str,
    user_id: str,
    idempotency_key: str,
    batch_id: Optional[str] = None,
) -> tuple[Voucher, bool]:
    """发放一张凭证。返回 (voucher, created)。重复提交返回 (原凭证, False)。"""
    from .db import tx
    with tx(conn):
        # 幂等重放：同一 key 直接返回首次结果
        row = conn.execute(
            "SELECT * FROM vouchers WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is not None:
            return _row_to_voucher(row), False

        crow = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if crow is None:
            raise BizError(ErrorCode.CAMPAIGN_NOT_FOUND, f"campaign {campaign_id} not found", 404)
        _apply_due_activation(conn, crow)
        crow = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if crow["status"] != CampaignStatus.ACTIVE.value:
            raise BizError(ErrorCode.CAMPAIGN_NOT_ACTIVE,
                           f"campaign is {crow['status']}, cannot issue", 409)

        # 每人限领（事务内检查，与插入原子）
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM vouchers WHERE campaign_id=? AND user_id=?",
            (campaign_id, user_id)).fetchone()["c"]
        if cnt >= crow["per_user_limit"]:
            raise BizError(ErrorCode.PER_USER_LIMIT_REACHED,
                           f"user {user_id} reached per-user limit", 409)

        # 总量：条件 UPDATE 原子扣减，rowcount=0 即额度耗尽
        cur = conn.execute(
            """UPDATE campaigns SET issued_count=issued_count+1, updated_at=?
               WHERE id=? AND (total_quota IS NULL OR issued_count < total_quota)""",
            (now_iso(), campaign_id))
        if cur.rowcount != 1:
            raise BizError(ErrorCode.QUOTA_EXHAUSTED, "campaign quota exhausted", 409)

        vid = uuid.uuid4().hex
        try:
            conn.execute(
                """INSERT INTO vouchers
                   (id, campaign_id, user_id, status, idempotency_key, rule_version,
                    issued_at, redeemed_at, redeem_idempotency_key, batch_id)
                   VALUES (?,?,?,?,?,?,?,NULL,NULL,?)""",
                (vid, campaign_id, user_id, VoucherStatus.ISSUED.value, idempotency_key,
                 crow["audience_rule_version"], now_iso(), batch_id),
            )
        except sqlite3.IntegrityError:
            # 并发下同 key 已被插入：回滚后返回已存在的那条
            raise _ReplayExisting(idempotency_key)
        row = conn.execute("SELECT * FROM vouchers WHERE id=?", (vid,)).fetchone()
        return _row_to_voucher(row), True


class _ReplayExisting(Exception):
    def __init__(self, key: str):
        self.key = key


def issue_safe(conn, campaign_id, user_id, idempotency_key, batch_id=None):
    """issue 的竞态安全包装：处理并发下同 key 插入冲突。"""
    try:
        return issue(conn, campaign_id, user_id, idempotency_key, batch_id)
    except _ReplayExisting as e:
        row = conn.execute(
            "SELECT * FROM vouchers WHERE idempotency_key=?", (e.key,)).fetchone()
        return _row_to_voucher(row), False


def redeem(conn: sqlite3.Connection, voucher_id: str,
           idempotency_key: str) -> tuple[Voucher, bool]:
    """核销。返回 (voucher, redeemed_now)。重复提交返回 (凭证, False)。"""
    from .db import tx
    with tx(conn):
        row = conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
        if row is None:
            raise BizError(ErrorCode.VOUCHER_NOT_FOUND, f"voucher {voucher_id} not found", 404)
        if row["status"] == VoucherStatus.REDEEMED.value:
            if row["redeem_idempotency_key"] == idempotency_key:
                return _row_to_voucher(row), False  # 同一请求重放
            raise BizError(ErrorCode.ALREADY_REDEEMED,
                           f"voucher {voucher_id} already redeemed", 409)
        cur = conn.execute(
            """UPDATE vouchers SET status=?, redeemed_at=?, redeem_idempotency_key=?
               WHERE id=? AND status=?""",
            (VoucherStatus.REDEEMED.value, now_iso(), idempotency_key,
             voucher_id, VoucherStatus.ISSUED.value))
        if cur.rowcount != 1:
            raise BizError(ErrorCode.ALREADY_REDEEMED,
                           f"voucher {voucher_id} already redeemed", 409)
        row = conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
        return _row_to_voucher(row), True
