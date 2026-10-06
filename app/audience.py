"""Audience rule versioning, deterministic selection and explanation.

Rules are immutable once stored: an "edit" creates a new version with a new
fingerprint.  A selection run stores the matched user ids together with a
``selection_hash`` computed from (version, fingerprint, sorted user ids),
so:

* the same rule over the same data reproduces the identical hash;
* changing a rule yields a new version, and the previous selection
  (including the vouchers issued against it) stays explainable.
"""
from __future__ import annotations

import json

from .authz import Actor
from .errors import NotFoundError
from .lifecycle import utcnow_iso
from .models import Rule
from .rule_engine import (UserProfile, evaluate, explain as explain_rule,
                          rule_fingerprint, selection_hash, validate_rule)
from .storage import Storage


def _row_to_rule(row) -> Rule:
    import datetime as _dt
    from .lifecycle import parse_ts
    return Rule(id=row["id"], campaign_id=row["campaign_id"],
                version=row["version"], body=json.loads(row["body"]),
                fingerprint=row["fingerprint"],
                created_at=parse_ts(row["created_at"]))


class AudienceService:
    def __init__(self, storage: Storage, authorizer=None):
        self.storage = storage
        self.authorizer = authorizer

    def upsert_rule(self, campaign_id: int, body: dict,
                    actor: Actor | None = None) -> Rule:
        # campaign must exist
        row = self.storage.query_one(
            "SELECT id FROM campaigns WHERE id=?", (campaign_id,))
        if row is None:
            raise NotFoundError(f"campaign {campaign_id} not found")
        validate_rule(body)
        if actor is not None and self.authorizer is not None:
            self.authorizer.authorize(actor, "rule.update", "campaign",
                                      campaign_id)
        fingerprint = rule_fingerprint(body)
        ts = utcnow_iso()
        with self.storage.tx() as conn:
            latest = conn.execute(
                "SELECT version, fingerprint FROM rules WHERE campaign_id=?"
                " ORDER BY version DESC LIMIT 1",
                (campaign_id,)).fetchone()
            if latest is not None and latest["fingerprint"] == fingerprint:
                # identical body re-submitted: no new version
                r = conn.execute(
                    "SELECT * FROM rules WHERE campaign_id=? AND version=?",
                    (campaign_id, latest["version"])).fetchone()
                return _row_to_rule(r)
            version = 1 if latest is None else latest["version"] + 1
            cur = conn.execute(
                "INSERT INTO rules(campaign_id, version, body, fingerprint,"
                " created_at) VALUES (?,?,?,?,?)",
                (campaign_id, version,
                 json.dumps(body, sort_keys=True, separators=(",", ":")),
                 fingerprint, ts))
            conn.execute(
                "UPDATE campaigns SET rule_version=?, updated_at=? WHERE id=?",
                (version, ts, campaign_id))
            return self.get_rule(campaign_id, version)

    def get_rule(self, campaign_id: int, version: int | None = None) -> Rule:
        if version is None:
            row = self.storage.query_one(
                "SELECT * FROM rules WHERE campaign_id=?"
                " ORDER BY version DESC LIMIT 1", (campaign_id,))
        else:
            row = self.storage.query_one(
                "SELECT * FROM rules WHERE campaign_id=? AND version=?",
                (campaign_id, version))
        if row is None:
            raise NotFoundError(
                f"rule v{version} for campaign {campaign_id} not found")
        return _row_to_rule(row)

    def list_versions(self, campaign_id: int) -> list[Rule]:
        rows = self.storage.query(
            "SELECT * FROM rules WHERE campaign_id=? ORDER BY version",
            (campaign_id,))
        return [_row_to_rule(r) for r in rows]

    def run_selection(self, campaign_id: int,
                      users: list[UserProfile]) -> dict:
        """Evaluate the *current* rule version over ``users`` deterministically.

        Returns a snapshot descriptor and persists it keyed by
        (campaign_id, rule_version).  Re-running the same version over the
        same data verifies the stored hash and returns the same snapshot.
        """
        rule = self.get_rule(campaign_id)
        matched = sorted(u.user_id for u in users if evaluate(rule.body, u))
        digest = selection_hash(rule.version, rule.fingerprint, matched)
        ts = utcnow_iso()
        with self.storage.tx() as conn:
            existing = conn.execute(
                "SELECT selection_hash FROM selection_snapshots"
                " WHERE campaign_id=? AND rule_version=?",
                (campaign_id, rule.version)).fetchone()
            if existing is not None:
                if existing["selection_hash"] != digest:
                    # A changed *dataset* is allowed, but the digest of the
                    # recorded run is authoritative; surface both.
                    return {"rule_version": rule.version,
                            "fingerprint": rule.fingerprint,
                            "selection_hash": digest,
                            "previous_hash": existing["selection_hash"],
                            "matched": matched,
                            "matched_count": len(matched),
                            "stable": False}
            else:
                conn.execute(
                    "INSERT INTO selection_snapshots(campaign_id, rule_version,"
                    " fingerprint, selection_hash, matched_count, user_ids,"
                    " created_at) VALUES (?,?,?,?,?,?,?)",
                    (campaign_id, rule.version, rule.fingerprint, digest,
                     len(matched), json.dumps(matched), ts))
        return {"rule_version": rule.version,
                "fingerprint": rule.fingerprint,
                "selection_hash": digest,
                "matched": matched,
                "matched_count": len(matched),
                "stable": True}

    def get_snapshot(self, campaign_id: int, version: int) -> dict:
        row = self.storage.query_one(
            "SELECT * FROM selection_snapshots WHERE campaign_id=? AND rule_version=?",
            (campaign_id, version))
        if row is None:
            raise NotFoundError(
                f"snapshot v{version} of campaign {campaign_id} not found")
        return {"rule_version": row["rule_version"],
                "fingerprint": row["fingerprint"],
                "selection_hash": row["selection_hash"],
                "matched": json.loads(row["user_ids"]),
                "matched_count": row["matched_count"]}

    def explain(self, campaign_id: int, version: int | None,
                user: UserProfile) -> dict:
        rule = self.get_rule(campaign_id, version)
        trace = explain_rule(rule.body, user)
        return {"user_id": user.user_id, "rule_version": rule.version,
                "fingerprint": rule.fingerprint, "trace": trace,
                "matched": bool(trace["result"])}
