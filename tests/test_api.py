import time
from datetime import datetime, timedelta, timezone

OP_HEADERS = {"X-Actor-Id": "op-1", "X-Actor-Role": "operator"}
USER_HEADERS = {"X-Actor-Id": "u1", "X-Actor-Role": "user"}


def test_full_flow_and_error_codes(client):
    r = client.post("/api/campaigns",
                    json={"name": "c", "total_quota": 2,
                          "per_user_quota": 1})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"

    r = client.post("/api/campaigns",
                    json={"name": "c", "total_quota": 2, "per_user_quota": 1},
                    headers=OP_HEADERS)
    cid = r.json()["id"]
    assert r.json()["status"] == "draft"

    # illegal transition
    r = client.post(f"/api/campaigns/{cid}/transition",
                    json={"target": "active"}, headers=OP_HEADERS)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_transition"
    for target in ("scheduled", "active"):
        client.post(f"/api/campaigns/{cid}/transition",
                    json={"target": target}, headers=OP_HEADERS)

    r = client.post(f"/api/campaigns/{cid}/vouchers",
                    json={"user_id": "u1", "request_id": "r-1"},
                    headers=USER_HEADERS)
    assert r.status_code == 201
    vid = r.json()["id"]
    # duplicate request
    r2 = client.post(f"/api/campaigns/{cid}/vouchers",
                     json={"user_id": "u1", "request_id": "r-1"},
                     headers=USER_HEADERS)
    assert r2.json()["id"] == vid
    # second voucher uses the remaining total slot
    r3 = client.post(f"/api/campaigns/{cid}/vouchers",
                     json={"user_id": "u2", "request_id": "r-2"},
                     headers=USER_HEADERS)
    assert r3.status_code == 201
    # third distinct request is rejected: total quota exhausted
    r4 = client.post(f"/api/campaigns/{cid}/vouchers",
                     json={"user_id": "u3", "request_id": "r-3"},
                     headers=USER_HEADERS)
    assert r4.status_code == 409
    assert r4.json()["error"]["code"] == "quota_exhausted"


def test_redeem_and_agent_flow(client):
    r = client.post("/api/campaigns",
                    json={"name": "c", "total_quota": 5,
                          "per_user_quota": 2}, headers=OP_HEADERS)
    cid = r.json()["id"]
    for t in ("scheduled", "active"):
        client.post(f"/api/campaigns/{cid}/transition",
                    json={"target": t}, headers=OP_HEADERS)
    r = client.post(f"/api/campaigns/{cid}/vouchers",
                    json={"user_id": "u1", "request_id": "v1"},
                    headers=USER_HEADERS)
    vid = r.json()["id"]
    # other user cannot redeem
    r = client.post(f"/api/vouchers/{vid}/redeem",
                    json={"request_id": "x", "user_id": "u9"},
                    headers={"X-Actor-Id": "u9", "X-Actor-Role": "user"})
    assert r.status_code == 403
    # agent 代客核销
    r = client.post(f"/api/vouchers/{vid}/redeem",
                    json={"request_id": "ag-1"},
                    headers={"X-Actor-Id": "ag-1", "X-Actor-Role": "agent"})
    assert r.status_code == 200 and r.json()["redeemed_by"] == "ag-1"
    # second redeem rejected
    r = client.post(f"/api/vouchers/{vid}/redeem",
                    json={"request_id": "ag-2"},
                    headers={"X-Actor-Id": "ag-1", "X-Actor-Role": "agent"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "voucher_not_redeemable"


def test_referral_endpoints(client):
    r = client.post("/api/referrals",
                    json={"referrer_id": "a", "referred_id": "a",
                          "campaign_id": 1})
    assert r.status_code == 409 and "self_referral" in r.text
    assert client.post("/api/referrals",
                       json={"referrer_id": "a", "referred_id": "b",
                             "campaign_id": 1}).status_code == 201
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    r = client.post("/api/conversions",
                    json={"event_id": "e1", "user_id": "b",
                          "campaign_id": 1, "occurred_at": future})
    assert r.json()["attributed"] is True


def test_batch_resume_endpoint(client):
    r = client.post("/api/campaigns",
                    json={"name": "c", "total_quota": 3,
                          "per_user_quota": 1}, headers=OP_HEADERS)
    cid = r.json()["id"]
    for t in ("scheduled", "active"):
        client.post(f"/api/campaigns/{cid}/transition",
                    json={"target": t}, headers=OP_HEADERS)
    r = client.post("/api/batches",
                    json={"campaign_id": cid, "idempotency_key": "k1",
                          "user_ids": ["u1", "u2", "u3", "u4"]},
                    headers=OP_HEADERS)
    job_id = r.json()["id"]
    r = client.post(f"/api/batches/{job_id}/run", json={"fail_after": 2})
    assert r.status_code == 500  # simulated crash
    r = client.post(f"/api/batches/{job_id}/resume")
    body = r.json()
    assert body["done"] == 3 and body["failed"] == 1
    # resubmitting same key does not create a new job
    r = client.post("/api/batches",
                    json={"campaign_id": cid, "idempotency_key": "k1",
                          "user_ids": ["u1", "u2", "u3", "u4"]},
                    headers=OP_HEADERS)
    assert r.json()["id"] == job_id


def test_audit_listing(client):
    client.post("/api/campaigns",
                json={"name": "c", "total_quota": 1, "per_user_quota": 1})
    r = client.get("/api/audit", params={"allowed": False})
    assert any(e["action"] == "campaign.create"
               for e in r.json()["entries"])
