"""End-to-end flow test with PostGrid stubbed out (no network).

Covers: key issuance -> draft (screen + verify + quote) -> request_send ->
approve (PENDING gate) -> SENT, plus reject path, double-approve guard,
screening block, and auth enforcement.
"""
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

FAKE_VERIFICATION = {"deliverability": "deliverable", "components": {}, "raw": {}}
FAKE_LETTER = {"id": "ltr_fake123", "expected_delivery_date": "2026-10-02"}


def issue_key(name="agent-1"):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["api_key"].startswith("lms_")
    return body["api_key"]


ADDR = {
    "name": "Jane Doe",
    "address_line1": "123 Main St",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}

KEY = issue_key()
H = {"X-API-Key": KEY}

with patch("mail_provider.verify_address", return_value=FAKE_VERIFICATION), patch(
    "mail_provider.create_letter", return_value=FAKE_LETTER
), patch("mail_provider.is_test_mode", return_value=True):
    # 1. draft -> quote math for a single letter
    r = c.post(
        "/v1/drafts",
        json={"to": ADDR, "from": ADDR, "html": "<p>Hello</p>", "quantity": 1},
        headers=H,
    )
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["quote"]["tier"] == "flat"
    assert draft["quote"]["service_fee_per_letter"] == 1.00
    draft_id = draft["draft_id"]

    # 2. quantity draft -> still flat $1/letter, no tiers
    r = c.post(
        "/v1/drafts",
        json={"to": ADDR, "from": ADDR, "html": "<p>Bulk</p>", "quantity": 10},
        headers=H,
    )
    assert r.json()["quote"]["tier"] == "flat"
    assert r.json()["quote"]["service_fee_per_letter"] == 1.00
    assert r.json()["quote"]["service_fee_total"] == 10.00
    bulk_draft_id = r.json()["draft_id"]

    # 3. request send -> PENDING, nothing mailed yet
    r = c.post("/v1/sends", json={"draft_id": draft_id}, headers=H)
    assert r.status_code == 200, r.text
    req_id = r.json()["request_id"]
    assert r.json()["status"] == "PENDING"

    # 4. status check
    r = c.get(f"/v1/sends/{req_id}", headers=H)
    assert r.json()["status"] == "PENDING"

    # 5. agent cannot approve (admin only)
    r = c.post(f"/v1/admin/sends/{req_id}/approve", headers=H)
    assert r.status_code == 403, r.status_code

    # 6. human approves -> SENT via PostGrid stub
    r = c.post(f"/v1/admin/sends/{req_id}/approve", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SENT"
    assert r.json()["provider_letter_id"] == "ltr_fake123"

    # 7. double-approve is rejected
    r = c.post(f"/v1/admin/sends/{req_id}/approve", headers=ADMIN)
    assert r.status_code == 409, r.status_code

    # 8. reject path
    r = c.post("/v1/sends", json={"draft_id": bulk_draft_id}, headers=H)
    rej_id = r.json()["request_id"]
    r = c.post(
        f"/v1/admin/sends/{rej_id}/reject",
        json={"reason": "not today"},
        headers=ADMIN,
    )
    assert r.json()["status"] == "REJECTED"

    # 9. pending list shows nothing left pending
    r = c.get("/v1/admin/sends?status=PENDING", headers=ADMIN)
    assert r.json()["sends"] == []

    # 10. history has audit entries
    r = c.get("/v1/admin/history", headers=ADMIN)
    actions = {e["action"] for e in r.json()["events"]}
    assert {"draft.create", "send.request", "send.approved", "send.rejected"} <= actions

# 11. screening blocks before any PostGrid call (no patches active -> would fail at PostGrid anyway)
r = c.post(
    "/v1/drafts",
    json={"to": ADDR, "from": ADDR, "html": "<p>send bitcoin now</p>"},
    headers=H,
)
assert r.status_code == 422, r.status_code

# 12. auth enforcement
assert c.post("/v1/drafts", json={}).status_code == 401
assert c.get("/v1/admin/sends", headers={"X-Admin-Token": "wrong"}).status_code == 403
assert c.get("/").status_code == 200  # dashboard serves

os.remove(DB)
print("API flow test passed ✔ (draft -> approve -> SENT, reject, screening, auth)")
