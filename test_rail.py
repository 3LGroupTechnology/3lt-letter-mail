"""Invisible-rail tests: one-time Connect authorization -> zero-prompt sends.

Covers: connect flow (session -> payment -> finalize) issues an on_command
key with per-send approval OFF bound to a monthly cap; sends flow with zero
approval prompts; exceeding the monthly cap -> 402 with nothing mailed or
charged; opting back into per-send approval restores the Allow/Deny link
flow; zero-prompt key with no payment method -> 402. Stripe fully mocked,
PostGrid stubbed, no network.
"""
import os
import sys
import tempfile
from urllib.parse import urlparse

DB = tempfile.mktemp(suffix=".db")
RECEIPTS = tempfile.mkdtemp(prefix="tlt_receipts_")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake123"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_fake123"
os.environ["TLT_MAILER_BACKEND"] = "log"
os.environ["TLT_RECEIPTS_DIR"] = RECEIPTS
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
os.environ["APPROVAL_TOKEN_SECRET"] = "test-approval-secret"

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(os.path.dirname(SERVICE_DIR), "3lt-shared")
sys.path.insert(0, SERVICE_DIR)
sys.path.insert(0, SHARED_DIR)


# ---- fake stripe -------------------------------------------------------------
class _FakeStripeError(Exception):
    pass


class _FakeCustomer:
    @staticmethod
    def create(**kw):
        return {"id": "cus_rail123", "email": kw.get("email")}


class _FakeSetupIntent:
    status = "succeeded"

    @staticmethod
    def create(**kw):
        return {"id": "seti_rail123", "client_secret": "seti_rail_secret"}

    @staticmethod
    def retrieve(si_id):
        return {"id": si_id, "status": _FakeSetupIntent.status}


class _FakePaymentIntent:
    create_calls = []
    capture_calls = []

    @staticmethod
    def create(**kw):
        _FakePaymentIntent.create_calls.append(kw)
        return {"id": "pi_rail1", "status": "requires_capture"}

    @staticmethod
    def capture(pid, **kw):
        _FakePaymentIntent.capture_calls.append((pid, kw))
        return {"id": pid, "status": "succeeded"}

    @staticmethod
    def cancel(pid, **kw):
        return {"id": pid, "status": "canceled"}


class FakeStripe:
    Customer = _FakeCustomer
    SetupIntent = _FakeSetupIntent
    PaymentIntent = _FakePaymentIntent
    error = type("error", (), {"StripeError": _FakeStripeError})
    api_key = None


sys.modules["stripe"] = FakeStripe

from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

FAKE_VERIFICATION = {"deliverability": "deliverable", "components": {}, "raw": {}}
provider_calls = []


def fake_create_letter(**kw):
    provider_calls.append(kw)
    return {"id": "ltr_rail1", "expected_delivery_date": "2026-10-02"}


ADDR = {
    "name": "Jane Doe",
    "address_line1": "123 Main St",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}


def connect_key(name="rail-agent", email="rail@example.com",
                monthly_cap_cents=20000):
    """Run the full Connect flow; return (key_id, raw_key)."""
    r = c.post("/v1/connect", json={"name": name, "owner_email": email,
                                    "monthly_cap_cents": monthly_cap_cents})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["monthly_cap_cents"] == monthly_cap_cents
    path = urlparse(body["connect_url"]).path
    token = path.rsplit("/", 1)[1]

    r = c.get(path)
    assert r.status_code == 200 and "Connect 3L-Group Technology" in r.text

    r = c.post(f"/v1/connect/{token}/setup-intent")
    assert r.status_code == 200, r.text
    assert r.json()["client_secret"] == "seti_rail_secret"
    assert r.json()["monthly_cap_cents"] == monthly_cap_cents

    r = c.post(f"/v1/connect/{token}/finalize",
               json={"setup_intent_id": "seti_rail123"})
    assert r.status_code == 200, r.text
    fin = r.json()
    assert fin["tier"] == "on_command"
    assert fin["require_per_send_approval"] is False
    # risk engine: new keys start at L0 ($50/mo), the requested $200/mo is
    # kept only as a lowerable customer ceiling (min(L0 cap, requested)).
    assert fin["monthly_cap_cents"] == 5000, fin
    assert fin["api_key"].startswith("lms_")

    # session is single-use
    r = c.post(f"/v1/connect/{token}/finalize",
               json={"setup_intent_id": "seti_rail123"})
    assert r.status_code == 404, r.status_code
    return fin["id"], fin["api_key"]


def make_draft(key_header, html="<p>Hello</p>"):
    r = c.post(
        "/v1/drafts",
        json={"to": ADDR, "from": ADDR, "html": html, "quantity": 1},
        headers=key_header,
    )
    assert r.status_code == 200, r.text
    return r.json()["draft_id"]


provider_patches = (
    patch("mail_provider.verify_address", return_value=FAKE_VERIFICATION),
    patch("mail_provider.create_letter", side_effect=fake_create_letter),
    patch("mail_provider.is_test_mode", return_value=True),
)
for p in provider_patches:
    p.start()
try:
    # --- 1. connect flow issues a zero-prompt rail key ---
    kid, raw = connect_key()
    H = {"X-API-Key": raw}
    row = db.get_key(kid)
    assert row["tier"] == "on_command"
    assert row["require_per_send_approval"] == 0
    assert row["monthly_cap_cents"] == 5000  # risk L0 ceiling
    assert row["stripe_customer_id"] == "cus_rail123"
    assert row["owner_email"] == "rail@example.com"
    spend = c.get(f"/v1/admin/keys/{kid}/spend", headers=ADMIN).json()
    assert spend["require_per_send_approval"] is False
    assert spend["monthly_cap_cents"] == 5000  # risk L0 ceiling
    assert spend["spent_month_cents"] == 0
    print("1. connect flow -> on_command rail key, per-send approval off ✔")

    # --- 2. three sends flow with zero approval prompts ---
    sent_ids = []
    for i in range(3):
        did = make_draft(H, html=f"<p>Rail letter {i}</p>")
        r = c.post("/v1/sends", json={"draft_id": did}, headers=H)
        body = r.json()
        assert r.status_code == 200, body
        assert body["status"] == "SENT", body
        assert body.get("approval_required") is False, body
        assert "approval_url" not in body, body  # no prompts, ever
        assert body["tier_decision"] == "within_guardrails"
        sent_ids.append(body["request_id"])
    assert len(_FakePaymentIntent.create_calls) == 3
    assert len(provider_calls) == 3
    spend = c.get(f"/v1/admin/keys/{kid}/spend", headers=ADMIN).json()
    assert spend["spent_month_cents"] == 3 * 234, spend  # grossed charges
    print("2. three sends, zero prompts, charged + mailed ✔")

    # --- 3. monthly cap: lower it so the 4th send -> 402 ---
    # 3 x 234c = 702c spent (gross); cap 800c lets none more through.
    r = c.post(f"/v1/admin/keys/{kid}/caps",
               json={"monthly_cap_cents": 800}, headers=ADMIN)
    assert r.status_code == 200, r.text
    provider_before = len(provider_calls)
    stripe_before = len(_FakePaymentIntent.create_calls)
    reqs_before = len(db.list_send_requests())
    did = make_draft(H, html="<p>Over the cap</p>")
    r = c.post("/v1/sends", json={"draft_id": did}, headers=H)
    assert r.status_code == 402, (r.status_code, r.text)
    assert "cap" in r.json()["detail"].lower()
    assert len(provider_calls) == provider_before, "nothing mailed over the cap"
    assert len(_FakePaymentIntent.create_calls) == stripe_before, \
        "nothing charged over the cap"
    assert len(db.list_send_requests()) == reqs_before, \
        "no send_request row on 402"
    print("3. over monthly cap -> 402, nothing mailed/charged/stored ✔")

    # --- 4. opting into per-send approval restores the Allow/Deny flow ---
    r = c.post(f"/v1/admin/keys/{kid}/per-send-approval",
               json={"require": True}, headers=ADMIN)
    assert r.json()["require_per_send_approval"] is True
    # raise the cap back up so the cap isn't what blocks this send
    c.post(f"/v1/admin/keys/{kid}/caps", json={"monthly_cap_cents": 20000},
           headers=ADMIN)
    did = make_draft(H, html="<p>Please approve me</p>")
    r = c.post("/v1/sends", json={"draft_id": did}, headers=H)
    body = r.json()
    assert body["status"] == "PENDING", body
    assert body["tier_decision"] == "per_send_approval_required"
    assert body["approval_required"] is True
    assert body["approval_url"].startswith("http://127.0.0.1:8000/a/")
    token = body["approval_url"].rsplit("/a/", 1)[1]
    r = c.post(f"/a/{token}/allow")
    assert r.status_code == 200 and "Approved" in r.text
    st = c.get(f"/v1/sends/{body['request_id']}", headers=H).json()
    assert st["status"] == "SENT" and st["billing"]["status"] == "succeeded"
    print("4. per-send approval opt-in -> Allow/Deny link flow ✔")

    # --- 5. zero-prompt key with no payment method -> 402 ---
    r = c.post("/v1/keys", json={"name": "no-pay"}, headers=ADMIN)
    kid5, raw5 = r.json()["id"], r.json()["api_key"]
    H5 = {"X-API-Key": raw5}
    c.post(f"/v1/admin/keys/{kid5}/tier", json={"tier": "on_command"},
           headers=ADMIN)
    c.post(f"/v1/admin/keys/{kid5}/per-send-approval",
           json={"require": False}, headers=ADMIN)
    did5 = make_draft(H5)
    r = c.post("/v1/sends", json={"draft_id": did5}, headers=H5)
    assert r.status_code == 402, (r.status_code, r.text)
    assert "payment method" in r.json()["detail"].lower()
    print("5. zero-prompt + no payment method -> 402 ✔")

    # --- 6. connect validation: bad email, bad token, unconfirmed setup ---
    r = c.post("/v1/connect",
               json={"name": "x", "owner_email": "not-an-email"})
    assert r.status_code == 400, r.status_code
    r = c.get("/connect/bogus-token")
    assert r.status_code == 404, r.status_code
    r = c.post("/v1/connect", json={"name": "unconf",
                                    "owner_email": "u@example.com"})
    token6 = urlparse(r.json()["connect_url"]).path.rsplit("/", 1)[1]
    _FakeSetupIntent.status = "requires_payment_method"
    try:
        r = c.post(f"/v1/connect/{token6}/finalize",
                   json={"setup_intent_id": "seti_rail123"})
        assert r.status_code == 402, (r.status_code, r.text)
    finally:
        _FakeSetupIntent.status = "succeeded"
    print("6. connect validation (email/token/unconfirmed) ✔")
finally:
    for p in provider_patches:
        p.stop()

os.remove(DB)
print("Rail tests passed ✔")
