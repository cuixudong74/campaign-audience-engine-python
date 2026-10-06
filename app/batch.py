"""Resumable batch voucher issuance.

Design:

* A batch is submitted once under an ``idempotency_key`` (UNIQUE).  All
  target users are written to ``batch_items`` in the *same* transaction as
  the job, so a failed submission leaves nothing behind and retrying the
  same key returns the existing job.
* Each item is processed independently: its issuance (or its terminal
  failure, e.g. per-user limit / quota exhausted / not in audience) is
  committed and the job's ``next_offset`` checkpoint advances in the same
  transaction.  There is therefore no half-written voucher and resuming
  picks up exactly at the checkpoint.
* Voucher issuance itself is idempotent by request id
  ``f"{job_id}:{idx}:{user_id}"``, so an item is never issued twice even
  if its commit is retried.
"""
from __future__ import annotations

from .authz import Actor
from .errors import BatchNotResumableError, NotFoundError
from .lifecycle import utcnow_iso
from .storage import Storage
from .vouchers import VoucherService

# item-level failures that are "normal" business rejections: the batch
# marks that item failed but continues processing the rest.
ITEM_REJECTION_CODES = {
    "quota_exhausted", "per_user_limit_reached", "campaign_not_active",
    "not_in_audience"}


class BatchService:
    def __init__(self, storage: Storage, vouchers: VoucherService | None = None,
                 authorizer=None):
        self.storage = storage
        self.vouchers = vouchers or VoucherService(storage, authorizer)
        self.authorizer = authorizer

    def submit(self, campaign_id: int, idempotency_key: str,
               user_ids: list[str], actor: Actor | None = None) -> dict:
        if actor is not None and self.authorizer is not None:
            self.authorizer.authorize(
                actor, "batch.run", "campaign", campaign_id,
                f"users={len(user_ids)}")
        ts = utcnow_iso()
        with self.storage.tx() as conn:
            existing = conn.execute(
                "SELECT id FROM batch_jobs WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if existing is not None:
                return self.get(existing["id"])
            camp = conn.execute(
                "SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if camp is None:
                raise NotFoundError(f"campaign {campaign_id} not found")
            cur = conn.execute(
                "INSERT INTO batch_jobs(campaign_id, idempotency_key, status,"
                " next_offset, total, created_at, updated_at)"
                " VALUES (?,?, 'pending', 0, ?, ?, ?)",
                (campaign_id, idempotency_key, len(user_ids), ts, ts))
            job_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO batch_items(job_id, idx, user_id, status)"
                " VALUES (?,?,?, 'pending')",
                [(job_id, i, uid) for i, uid in enumerate(user_ids)])
        return self.get(job_id)

    def run(self, job_id: int, fail_after: int | None = None) -> dict:
        """Process pending items from the checkpoint.

        ``fail_after`` (testing hook) simulates a crash after that many
        successfully processed items, raising RuntimeError after the
        checkpoint has advanced; ``resume`` then continues.
        """
        processed = 0
        while True:
            item = self._next_pending(job_id)
            if item is None:
                self._finish(job_id)
                return self.get(job_id)
            self._process_one(job_id, item)
            processed += 1
            if fail_after is not None and processed >= fail_after:
                # simulate process crash between items: state is durable,
                # nothing is in flight
                raise RuntimeError("simulated mid-batch crash")

    def resume(self, job_id: int) -> dict:
        job = self.get(job_id)
        if job["status"] in ("completed", "completed_with_errors"):
            return job
        if job["status"] not in ("pending", "running", "failed"):
            raise BatchNotResumableError(
                f"job {job_id} in status {job['status']} cannot be resumed")
        return self.run(job_id)

    # ------------------------------------------------------------- internals
    def _next_pending(self, job_id: int):
        with self.storage.tx() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='running', updated_at=?"
                " WHERE id=? AND status='pending'", (utcnow_iso(), job_id))
            row = conn.execute(
                "SELECT * FROM batch_items WHERE job_id=? AND status='pending'"
                " ORDER BY idx LIMIT 1", (job_id,)).fetchone()
            return row

    def _process_one(self, job_id: int, item) -> None:
        request_id = f"batch:{job_id}:{item['idx']}:{item['user_id']}"
        ts = utcnow_iso()
        with self.storage.tx() as conn:
            # re-read job row for campaign_id under lock
            job = conn.execute(
                "SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
            try:
                voucher = self.vouchers.issue(
                    job["campaign_id"], item["user_id"], request_id)
                conn.execute(
                    "UPDATE batch_items SET status='done', voucher_id=?,"
                    " error=NULL WHERE id=?", (voucher.id, item["id"]))
            except Exception as exc:  # item-level rejection
                code = getattr(exc, "code", exc.__class__.__name__)
                if code not in ITEM_REJECTION_CODES:
                    raise
                conn.execute(
                    "UPDATE batch_items SET status='failed', error=?"
                    " WHERE id=?", (code, item["id"]))
            conn.execute(
                "UPDATE batch_jobs SET next_offset=?, updated_at=? WHERE id=?",
                (item["idx"] + 1, ts, job_id))

    def _finish(self, job_id: int) -> None:
        with self.storage.tx() as conn:
            failed = conn.execute(
                "SELECT COUNT(*) AS n FROM batch_items WHERE job_id=? AND status='failed'",
                (job_id,)).fetchone()["n"]
            conn.execute(
                "UPDATE batch_jobs SET status=?, error=NULL, updated_at=?"
                " WHERE id=?",
                ("completed" if failed == 0 else "completed_with_errors",
                 utcnow_iso(), job_id))

    def get(self, job_id: int) -> dict:
        row = self.storage.query_one(
            "SELECT * FROM batch_jobs WHERE id=?", (job_id,))
        if row is None:
            raise NotFoundError(f"batch job {job_id} not found")
        items = [dict(r) for r in self.storage.query(
            "SELECT idx, user_id, status, voucher_id, error FROM batch_items"
            " WHERE job_id=? ORDER BY idx", (job_id,))]
        done = sum(1 for i in items if i["status"] == "done")
        failed = sum(1 for i in items if i["status"] == "failed")
        return {"id": row["id"], "campaign_id": row["campaign_id"],
                "idempotency_key": row["idempotency_key"],
                "status": row["status"], "next_offset": row["next_offset"],
                "total": row["total"], "done": done, "failed": failed,
                "error": row["error"], "items": items}
