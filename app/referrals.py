"""Referral registration and out-of-order conversion attribution.

* Self referral (A -> A) is rejected.
* A new edge A -> B is rejected when it would close a cycle, i.e. there is
  already a directed valid path B -> A (this includes the simple mutual
  case A->B and B->A).
* Every conversion is stored with its original ``occurred_at``; events may
  arrive out of order or late.  Attribution requires the referral to have
  existed *before* the conversion occurred (deterministic event-time
  semantics, independent of arrival order).
* A valid referral is attributed at most once, enforced by a partial UNIQUE
  index on ``attributions(referral_id) WHERE attributed=1``.  Duplicate
  delivery of the same event is idempotent via ``event_id``.
"""
from __future__ import annotations

import sqlite3

from .authz import Actor, normalize_ts
from .errors import ReferralRejectedError
from .lifecycle import utcnow_iso
from .storage import Storage


class ReferralService:
    def __init__(self, storage: Storage, authorizer=None):
        self.storage = storage
        self.authorizer = authorizer

    # ------------------------------------------------------------- register
    def register(self, referrer_id: str, referred_id: str, campaign_id: int,
                 actor: Actor | None = None) -> dict:
        if actor is not None and self.authorizer is not None:
            self.authorizer.authorize(
                actor, "referral.register", "campaign", campaign_id,
                f"{referrer_id}->{referred_id}")
        reason = None
        if referrer_id == referred_id:
            reason = "self_referral"
        ts = utcnow_iso()
        try:
            with self.storage.tx() as conn:
                # idempotent: identical valid edge already exists
                existing = conn.execute(
                    "SELECT * FROM referrals WHERE referrer_id=? AND referred_id=?"
                    " AND campaign_id=? AND status='valid'",
                    (referrer_id, referred_id, campaign_id)).fetchone()
                if existing is not None:
                    return {"referral_id": existing["id"], "status": "valid",
                            "reason": "already_registered"}
                if reason is None:
                    reason = self._cycle_reason(
                        conn, referrer_id, referred_id, campaign_id)
                cur = conn.execute(
                    "INSERT INTO referrals(referrer_id, referred_id,"
                    " campaign_id, status, reject_reason, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (referrer_id, referred_id, campaign_id,
                     "valid" if reason is None else "rejected", reason, ts))
                if reason is not None:
                    raise ReferralRejectedError(
                        f"referral {referrer_id}->{referred_id} rejected:"
                        f" {reason}")
                return {"referral_id": cur.lastrowid, "status": "valid",
                        "reason": None}
        except sqlite3.IntegrityError:
            # concurrent insertion of the same valid edge
            row = self.storage.query_one(
                "SELECT * FROM referrals WHERE referrer_id=? AND referred_id=?"
                " AND campaign_id=? AND status='valid'",
                (referrer_id, referred_id, campaign_id))
            if row is not None:
                return {"referral_id": row["id"], "status": "valid",
                        "reason": "already_registered"}
            raise

    @staticmethod
    def _cycle_reason(conn, referrer_id: str, referred_id: str,
                      campaign_id: int) -> str | None:
        """Reject A->B if B can already reach A (the new edge closes a cycle).

        Edges run referrer -> referred.  If the referred user can already
        reach the referrer through existing valid edges, adding A->B closes
        a directed cycle.
        """
        seen: set[str] = set()
        frontier = [referred_id]
        while frontier:
            node = frontier.pop()
            if node == referrer_id:
                return "cyclic_referral"
            if node in seen:
                continue
            seen.add(node)
            rows = conn.execute(
                "SELECT referred_id FROM referrals WHERE referrer_id=?"
                " AND campaign_id=? AND status='valid'",
                (node, campaign_id)).fetchall()
            frontier.extend(r["referred_id"] for r in rows)
        return None

    # ----------------------------------------------------------- conversion
    def record_conversion(self, event_id: str, user_id: str, campaign_id: int,
                          occurred_at: str) -> dict:
        """Record a conversion and attribute it deterministically.

        Safe to call with events in any order and with late arrivals: the
        stored outcome for a given ``event_id`` never changes on replay.
        """
        received = utcnow_iso()
        occurred_at = normalize_ts(occurred_at)
        with self.storage.tx() as conn:
            dup = conn.execute(
                "SELECT * FROM conversion_events WHERE event_id=?",
                (event_id,)).fetchone()
            prior = conn.execute(
                "SELECT * FROM attributions WHERE event_id=?",
                (event_id,)).fetchone()
            if dup is not None:
                if prior is not None:
                    return {"event_id": event_id,
                            "attributed": bool(prior["attributed"]),
                            "referral_id": prior["referral_id"],
                            "reason": prior["reason"], "duplicate": True}
                # event row without attribution should not happen, but be safe
                return {"event_id": event_id, "attributed": False,
                        "reason": "duplicate", "duplicate": True}
            conn.execute(
                "INSERT INTO conversion_events(event_id, user_id, campaign_id,"
                " occurred_at, received_at) VALUES (?,?,?,?,?)",
                (event_id, user_id, campaign_id, occurred_at, received))

            referral = conn.execute(
                "SELECT * FROM referrals WHERE referred_id=? AND campaign_id=?"
                " AND status='valid' AND created_at <= ?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (user_id, campaign_id, occurred_at)).fetchone()
            if referral is None:
                reason = "no_referral"
                referral_id = 0
                attributed = False
            else:
                already = conn.execute(
                    "SELECT 1 FROM attributions WHERE referral_id=?"
                    " AND attributed=1", (referral["id"],)).fetchone()
                if already is not None:
                    reason = "referral_already_attributed"
                    referral_id = referral["id"]
                    attributed = False
                else:
                    reason = None
                    referral_id = referral["id"]
                    attributed = True
            try:
                conn.execute(
                    "INSERT INTO attributions(referral_id, event_id,"
                    " attributed, reason, created_at) VALUES (?,?,?,?,?)",
                    (referral_id, event_id, 1 if attributed else 0,
                     reason, received))
            except sqlite3.IntegrityError:
                # concurrent attribution of same referral/event
                row = conn.execute(
                    "SELECT * FROM attributions WHERE event_id=?",
                    (event_id,)).fetchone()
                if row is not None:
                    return {"event_id": event_id,
                            "attributed": bool(row["attributed"]),
                            "referral_id": row["referral_id"],
                            "reason": row["reason"], "duplicate": True}
                return {"event_id": event_id, "attributed": False,
                        "reason": "attributed_concurrently", "duplicate": True}
            return {"event_id": event_id, "attributed": attributed,
                    "referral_id": referral_id if attributed else None,
                    "reason": reason, "duplicate": False}

    def attribution_for(self, event_id: str) -> dict | None:
        row = self.storage.query_one(
            "SELECT * FROM attributions WHERE event_id=?", (event_id,))
        if row is None:
            return None
        return {"event_id": row["event_id"],
                "attributed": bool(row["attributed"]),
                "referral_id": row["referral_id"] if row["attributed"] else None,
                "reason": row["reason"]}
