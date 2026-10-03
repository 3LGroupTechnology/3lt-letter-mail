"""Gross-up (processing pass-through) tests.

Rule under test: 3LT's fee is always NET; Stripe's 2.9% + $0.30 rides on
top, baked invisibly into the single customer-facing total.

Covers: gross_up math on known values; the kept-after-fees invariant;
full send flows (single + ten-letter) where the Stripe PaymentIntent is created
for the gross amount, the receipt breaks out base / fee / processing /
total, the 3LT fee line is exactly whole, and the pre-send quote equals
the amount actually charged. Stripe + PostGrid fully mocked, no network.
"""
import math
import os
import sys
import tempfile

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
class _FakePaymentIntent:
    create_calls = []
    capture_calls = []

    @staticmethod
    def create(**kw):
        _FakePaymentIntent.create_calls.append(kw)
        return {"id": "pi_gross1", "status": "requires_capture"}

    @staticmethod
    def capture(pid, **kw):
        _FakePaymentIntent.capture_calls.append((pid, kw))
        return {"id": pid, "status": "succeeded"}

    @staticmethod
    def cancel(pid, **kw):
        return {"id": pid, "status": "canceled"}


class _FakeCustomer:
    @staticmethod
    def create(**kw):
        return {"id": "cus_gross123", "email": kw.get("email")}


class _FakeSetupIntent:
    @staticmethod
    def create(**kw):
        return {"id": "seti_gross123", "client_secret": "seti_secret"}


class FakeStripe:
    Customer = _FakeCustomer
    SetupIntent = _FakeSetupIntent
    PaymentIntent = _FakePaymentIntent
    api_key = None


sys.modules["stripe"] = FakeStripe

from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402
import tlt_billing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

FAKE_VERIFICATION = {"deliverability": "deliverable", "components": {}, "raw": {}}
FAKE_LETTER = {"id": "ltr_gross123", "expected_delivery_date": "2026-10-02"}
ADDR = {
    "name": "Jane Doe",
    "address_line1": "123 Main St",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}


def issue_key(name):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["api_key"]


def prep_rail_key(name, email):
    kid, raw = issue_key(name)
    H = {"X-API-Key": raw}
    assert c.post(f"/v1/admin/keys/{kid}/tier", json={"tier": "on_command"},
                  headers=ADMIN).status_code == 200
    assert c.post(f"/v1/admin/keys/{kid}/per-send-approval",
                  json={"require": False}, headers=ADMIN).status_code == 200
    assert c.post(f"/v1/admin/keys/{kid}/email",
                  json={"owner_email": email}, headers=ADMIN).status_code == 200
    r = c.post(f"/v1/admin/keys/{kid}/stripe-customer", json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return kid, H


def make_draft_full(key_header, quantity=1):
    r = c.post(
        "/v1/drafts",
        json={"to": ADDR, "from": ADDR, "html": "<p>Hi</p>",
              "quantity": quantity},
        headers=key_header,
    )
    assert r.status_code == 200, r.text
    return r.json()


def receipt_blobs(before):
    after = sorted(os.listdir(RECEIPTS))
    new = [f for f in after if f not in before]
    return "\n".join(open(os.path.join(RECEIPTS, f)).read() for f in new)


# --- 1. gross_up math ----------------------------------------------------------
assert tlt_billing.gross_up(208) == 246, tlt_billing.gross_up(208)     # $2.08 -> $2.46
assert tlt_billing.gross_up(1430) == 1504, tlt_billing.gross_up(1430)   # $14.30 -> $15.04
assert tlt_billing.gross_up(0) == 31  # ceil(30 / 0.971)
assert tlt_billing.PROCESSING_RATE == 0.029
assert tlt_billing.PROCESSING_FIXED_CENTS == 30

# kept-after-fees invariant: after Stripe takes 2.9% (rounded) + 30c,
# we always keep >= net — the margin is never eroded.
for net in (1, 50, 100, 208, 246, 1000, 1430, 14300, 99999):
    gross = tlt_billing.gross_up(net)
    kept = gross - round(gross * tlt_billing.PROCESSING_RATE) \
        - tlt_billing.PROCESSING_FIXED_CENTS
    assert kept >= net, (net, gross, kept)

# price_breakdown parts are consistent
parts = tlt_billing.price_breakdown(208)
assert parts == {"net_cents": 208, "processing_cents": 38, "gross_cents": 246}, parts

# negative nets are rejected
try:
    tlt_billing.gross_up(-5)
    raise AssertionError("expected BillingError for negative net")
except tlt_billing.BillingError:
    pass
print("1. gross_up math + kept-after-fees invariant ✔")

provider_patches = (
    patch("mail_provider.verify_address", return_value=FAKE_VERIFICATION),
    patch("mail_provider.create_letter", return_value=FAKE_LETTER),
    patch("mail_provider.is_test_mode", return_value=True),
)
for p in provider_patches:
    p.start()
try:
    # --- 2. single-letter flow: quote, charge, receipt all gross; fee whole ---
    kid, H = prep_rail_key("gross-key", "gross@example.com")
    before = sorted(os.listdir(RECEIPTS))
    d = make_draft_full(H, quantity=1)
    q = d["quote"]
    assert q["estimated_total"] == 1.97, q            # NET unchanged
    assert q["processing_fee_total"] == 0.37, q        # 234 - 197
    assert q["total_charged"] == 2.34, q               # customer-facing gross

    r = c.post("/v1/sends", json={"draft_id": d["draft_id"]}, headers=H)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "SENT", body
    # pre-send quote equals the amount actually charged — no surprises
    assert body["quote"]["total_charged"] == 2.34, body["quote"]
    assert body["quote"]["estimated_total"] == 1.97

    # Stripe PaymentIntent created for the GROSS figure
    assert _FakePaymentIntent.create_calls, "expected a Stripe charge"
    assert _FakePaymentIntent.create_calls[-1]["amount"] == 234, \
        _FakePaymentIntent.create_calls[-1]
    assert math.isclose(body["quote"]["total_charged"] * 100,
                        _FakePaymentIntent.create_calls[-1]["amount"])

    receipts = db.get_receipts_for_request(body["request_id"])
    assert len(receipts) == 1 and receipts[0]["amount_cents"] == 234

    # 4-line receipt: base / fee / processing / total; fee exactly whole
    blob = receipt_blobs(before)
    assert "Provider base (est.): $0.97" in blob, blob
    assert "3L-Group service fee: $1.00" in blob, blob   # full $1.00, never eroded
    assert "Processing: $0.37" in blob, blob
    assert "Total: $2.34" in blob, blob
    print("2. single-letter flow: gross charge, whole fee, 4-line receipt ✔")

    # --- 3. ten-letter flow: flat $1/letter fee stays whole ---
    kid2, H2 = prep_rail_key("gross-bulk", "bulk@example.com")
    before = sorted(os.listdir(RECEIPTS))
    d2 = make_draft_full(H2, quantity=10)
    q2 = d2["quote"]
    assert q2["estimated_total"] == 19.70, q2           # net: 10 x ($0.97 + $1.00)
    assert q2["service_fee_total"] == 10.00, q2
    assert q2["total_charged"] == 20.60, q2             # gross_up(1970) = 2060

    r = c.post("/v1/sends", json={"draft_id": d2["draft_id"]}, headers=H2)
    body2 = r.json()
    assert r.status_code == 200 and body2["status"] == "SENT", body2
    assert _FakePaymentIntent.create_calls[-1]["amount"] == 2060
    receipts2 = db.get_receipts_for_request(body2["request_id"])
    assert receipts2[0]["amount_cents"] == 2060

    blob2 = receipt_blobs(before)
    assert "Provider base (est.): $9.70" in blob2, blob2
    assert "3L-Group service fee: $10.00" in blob2, blob2  # full $1.00 x 10
    assert "Processing: $0.90" in blob2, blob2             # 2060 - 1970
    assert "Total: $20.60" in blob2, blob2
    print("3. ten-letter flow: $1/letter fee whole, gross charged ✔")

    # --- 4. Allow/Deny page shows the gross total, not the net ---
    kid3, raw3 = issue_key("gross-link")
    H3 = {"X-API-Key": raw3}
    assert c.post(f"/v1/admin/keys/{kid3}/email",
                  json={"owner_email": "l@example.com"},
                  headers=ADMIN).status_code == 200
    assert c.post(f"/v1/admin/keys/{kid3}/stripe-customer", json={},
                  headers=ADMIN).status_code == 200
    d3 = make_draft_full(H3, quantity=1)
    r = c.post("/v1/sends", json={"draft_id": d3["draft_id"]}, headers=H3)
    assert r.json()["status"] == "PENDING", r.json()
    assert r.json()["quote"]["total_charged"] == 2.34
    token = r.json()["approval_url"].rsplit("/a/", 1)[1]
    page = c.get(f"/a/{token}")
    assert page.status_code == 200
    assert "$2.34" in page.text, "approval page must show the gross total"
    assert "Processing" in page.text
    print("4. approval page shows gross total ✔")
finally:
    for p in provider_patches:
        p.stop()

os.remove(DB)
print("Gross-up tests passed ✔")
