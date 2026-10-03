"""Billing + trust tiers + one-tap approval tests (Stripe fully mocked).

Covers: on_command auto-approve within caps -> mocked Stripe charge -> receipt
row + emailed receipt file; over-cap -> PENDING; flagged content -> PENDING;
Allow link approves+charges (single use); Deny rejects with no charge and no
receipt; idempotent charge on retry; anomaly rule -> PENDING; webhook audit.
No network calls anywhere.
"""
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
class _FakeStripeError(Exception):
    pass


class _FakeCustomer:
    @staticmethod
    def create(**kw):
        return {"id": "cus_fake123", "email": kw.get("email")}


class _FakeSetupIntent:
    @staticmethod
    def create(**kw):
        return {"id": "seti_fake123", "client_secret": "seti_fake_secret_abc"}


class _FakePaymentIntent:
    create_calls = []
    capture_calls = []
    cancel_calls = []

    @staticmethod
    def create(**kw):
        _FakePaymentIntent.create_calls.append(kw)
        return {"id": "pi_fake1", "status": "requires_capture"}

    @staticmethod
    def capture(pid, **kw):
        _FakePaymentIntent.capture_calls.append((pid, kw))
        return {"id": pid, "status": "succeeded"}

    @staticmethod
    def cancel(pid, **kw):
        _FakePaymentIntent.cancel_calls.append((pid, kw))
        return {"id": pid, "status": "canceled"}


class _FakeWebhook:
    @staticmethod
    def construct_event(payload, sig, secret):
        if sig != "valid-sig":
            raise _FakeStripeError("bad signature")
        return {"id": "evt_fake", "type": "payment_intent.succeeded"}


class FakeStripe:
    Customer = _FakeCustomer
    SetupIntent = _FakeSetupIntent
    PaymentIntent = _FakePaymentIntent
    Webhook = _FakeWebhook
    error = type("error", (), {"StripeError": _FakeStripeError})
    api_key = None


sys.modules["stripe"] = FakeStripe

from unittest.mock import Mock, patch  # noqa: E402

import api  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402
import tlt_billing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

FAKE_VERIFICATION = {"deliverability": "deliverable", "components": {}, "raw": {}}
FAKE_LETTER = {"id": "ltr_fake123", "expected_delivery_date": "2026-10-02"}

ADDR = {
    "name": "Jane Doe",
    "address_line1": "123 Main St",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}


def reset_stripe_calls():
    _FakePaymentIntent.create_calls.clear()
    _FakePaymentIntent.capture_calls.clear()
    _FakePaymentIntent.cancel_calls.clear()


def issue_key(name):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], body["api_key"]


def set_zero_prompt(kid):
    """Opt an admin-issued key out of per-send approval (the rail default)."""
    r = c.post(f"/v1/admin/keys/{kid}/per-send-approval",
               json={"require": False}, headers=ADMIN)
    assert r.status_code == 200, r.text


def make_draft(key_header, html="<p>Hello</p>", quantity=1):
    r = c.post(
        "/v1/drafts",
        json={"to": ADDR, "from": ADDR, "html": html, "quantity": quantity},
        headers=key_header,
    )
    assert r.status_code == 200, r.text
    return r.json()["draft_id"]


def receipt_files():
    return sorted(os.listdir(RECEIPTS))


provider_patches = patch("mail_provider.verify_address", return_value=FAKE_VERIFICATION), patch(
    "mail_provider.create_letter", return_value=FAKE_LETTER
), patch("mail_provider.is_test_mode", return_value=True)


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


_enter(provider_patches)
try:
    # --- 1. on_command within caps: auto-approve -> Stripe charge -> receipt ---
    reset_stripe_calls()
    kid, raw = issue_key("auto-key")
    H = {"X-API-Key": raw}
    assert c.post(f"/v1/admin/keys/{kid}/tier", json={"tier": "on_command"},
                  headers=ADMIN).status_code == 200
    set_zero_prompt(kid)
    assert c.post(f"/v1/admin/keys/{kid}/email",
                  json={"owner_email": "owner@example.com"},
                  headers=ADMIN).status_code == 200
    r = c.post(f"/v1/admin/keys/{kid}/stripe-customer", json={"name": "Owner"},
               headers=ADMIN)
    assert r.status_code == 200 and r.json()["stripe_customer_id"] == "cus_fake123"
    r = c.post(f"/v1/admin/keys/{kid}/setup-intent", headers=ADMIN)
    assert r.json()["client_secret"] == "seti_fake_secret_abc"

    files_before = receipt_files()
    did = make_draft(H)
    r = c.post("/v1/sends", json={"draft_id": did}, headers=H)
    body = r.json()
    assert body["status"] == "SENT", body
    assert body["stripe_payment_id"] == "pi_fake1"
    assert body["tier"] == "on_command"
    req_id = body["request_id"]

    # idempotency keys were deterministic
    idem_keys = [kw["idempotency_key"] for kw in _FakePaymentIntent.create_calls]
    assert idem_keys == [f"sendreq_{req_id}:auth"], idem_keys
    assert _FakePaymentIntent.capture_calls[0][1]["idempotency_key"] == \
        f"sendreq_{req_id}:capture"

    receipts = db.get_receipts_for_request(req_id)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "succeeded"
    assert receipts[0]["stripe_payment_id"] == "pi_fake1"
    assert receipts[0]["amount_cents"] == 234  # $2.34 gross (197c net + processing)

    # emailed receipt file contains the charge id
    new_files = [f for f in receipt_files() if f not in files_before]
    assert new_files, "expected a receipt file on successful send"
    blob = "\n".join(
        open(os.path.join(RECEIPTS, f)).read() for f in new_files)
    assert "pi_fake1" in blob
    assert "To: owner@example.com" in blob

    st = c.get(f"/v1/sends/{req_id}", headers=H).json()
    assert st["billing"]["stripe_payment_id"] == "pi_fake1"
    assert st["tier"] == "on_command"
    assert st["tier_decision"] == "auto:within_guardrails"
    print("1. on_command auto-approve + charge + receipt ✔")

    # --- 2. over per-send cap -> PENDING, no charge ---
    reset_stripe_calls()
    kid2, raw2 = issue_key("cap-key")
    H2 = {"X-API-Key": raw2}
    c.post(f"/v1/admin/keys/{kid2}/tier", json={"tier": "on_command"}, headers=ADMIN)
    set_zero_prompt(kid2)
    c.post(f"/v1/admin/keys/{kid2}/stripe-customer", json={}, headers=ADMIN)
    c.post(f"/v1/admin/keys/{kid2}/caps", json={"per_send_cap_cents": 100},
           headers=ADMIN)
    did2 = make_draft(H2)  # $2.34 = 234c gross > 100c cap
    r = c.post("/v1/sends", json={"draft_id": did2}, headers=H2)
    body = r.json()
    assert body["status"] == "PENDING", body
    assert body["tier_decision"] == "over_per_send_cap"
    assert body["approval_url"].startswith("http://127.0.0.1:8000/a/")
    assert _FakePaymentIntent.create_calls == []
    print("2. over per-send cap -> PENDING, no charge ✔")

    # --- 3. flagged content on on_command key -> PENDING ---
    kid3, raw3 = issue_key("flag-key")
    H3 = {"X-API-Key": raw3}
    c.post(f"/v1/admin/keys/{kid3}/tier", json={"tier": "on_command"}, headers=ADMIN)
    set_zero_prompt(kid3)
    c.post(f"/v1/admin/keys/{kid3}/stripe-customer", json={}, headers=ADMIN)
    with patch("screen.screen_content",
               side_effect=[(True, ""), (False, "blocked phrase matched: 'xyz'")]):
        did3 = make_draft(H3, html="<p>clean at draft</p>")
        r = c.post("/v1/sends", json={"draft_id": did3}, headers=H3)
    body = r.json()
    assert body["status"] == "PENDING", body
    assert body["tier_decision"].startswith("flagged:"), body
    assert _FakePaymentIntent.create_calls == []
    print("3. flagged content -> PENDING human review ✔")

    # --- 4. Allow link approves + charges; second use rejected ---
    reset_stripe_calls()
    kid4, raw4 = issue_key("link-key")
    H4 = {"X-API-Key": raw4}
    c.post(f"/v1/admin/keys/{kid4}/email",
           json={"owner_email": "linkowner@example.com"}, headers=ADMIN)
    c.post(f"/v1/admin/keys/{kid4}/stripe-customer", json={}, headers=ADMIN)
    files_before = receipt_files()
    did4 = make_draft(H4)
    r = c.post("/v1/sends", json={"draft_id": did4}, headers=H4)
    assert r.json()["status"] == "PENDING"
    req4_id = r.json()["request_id"]
    token = r.json()["approval_url"].rsplit("/a/", 1)[1]

    r = c.get(f"/a/{token}")
    assert r.status_code == 200 and "Allow" in r.text and "Deny" in r.text
    assert "$2.34" in r.text  # approval page shows the gross customer total

    r = c.post(f"/a/{token}/allow")
    assert r.status_code == 200 and "Approved" in r.text
    st = c.get(f"/v1/sends/{req4_id}", headers=H4).json()
    assert st["status"] == "SENT" and st["approved_by"] == "approval_link"
    assert _FakePaymentIntent.create_calls, "expected a Stripe charge on Allow"
    new_files = [f for f in receipt_files() if f not in files_before]
    assert new_files and "pi_fake1" in "\n".join(
        open(os.path.join(RECEIPTS, f)).read() for f in new_files)

    r = c.post(f"/a/{token}/allow")
    assert r.status_code == 410, r.status_code  # single use enforced
    print("4. Allow link approves+charges; double-use -> 410 ✔")

    # --- 5. Deny rejects: no charge, no receipt email ---
    reset_stripe_calls()
    files_before = receipt_files()
    did5 = make_draft(H4)
    r = c.post("/v1/sends", json={"draft_id": did5}, headers=H4)
    token5 = r.json()["approval_url"].rsplit("/a/", 1)[1]
    r = c.post(f"/a/{token5}/deny")
    assert r.status_code == 200 and "Denied" in r.text
    assert _FakePaymentIntent.create_calls == []
    assert [f for f in receipt_files() if f not in files_before] == []
    print("5. Deny -> REJECTED, no charge, no receipt ✔")

    # --- 6. idempotent charge on retry ---
    reset_stripe_calls()
    fulfill = Mock(return_value={"id": "ltr_retry"})
    key_row = db.get_key(kid)  # has stripe customer
    did6 = make_draft(H)
    rid6 = db.insert_send_request(did6, 208)
    res1, rec1 = tlt_billing.settle(db, key_row, rid6, 208, "usd", fulfill, "test")
    res2, rec2 = tlt_billing.settle(db, key_row, rid6, 208, "usd", fulfill, "test")
    assert len(_FakePaymentIntent.create_calls) == 1
    assert fulfill.call_count == 1
    assert rec1["id"] == rec2["id"] and rec2["status"] == "succeeded"
    print("6. idempotent charge on retry ✔")

    # --- 7. anomaly rule: >3x 7-day average -> PENDING ---
    kid7, raw7 = issue_key("anomaly-key")
    H7 = {"X-API-Key": raw7}
    c.post(f"/v1/admin/keys/{kid7}/tier", json={"tier": "on_command"}, headers=ADMIN)
    set_zero_prompt(kid7)
    c.post(f"/v1/admin/keys/{kid7}/stripe-customer", json={}, headers=ADMIN)
    with db.connect() as conn:
        # 7 sends across days -1..-6 (two on day -1): avg = 1.0/day, and no
        # row sits exactly on the 7-day boundary (avoids timing flakiness).
        for i in [1, 1, 2, 3, 4, 5, 6]:
            d = conn.execute(
                "INSERT INTO drafts (api_key_id, to_json, from_json, html, quote_json)"
                " VALUES (?, '{}', '{}', '<p>x</p>', '{\"estimated_total\": 1.97}')",
                (kid7,)).lastrowid
            s = conn.execute(
                "INSERT INTO send_requests (draft_id, status, amount_cents) "
                "VALUES (?, 'SENT', 197)", (d,)).lastrowid
            conn.execute(
                "UPDATE send_requests SET created_at = datetime('now', ?), "
                "decided_at = datetime('now', ?) WHERE id = ?",
                (f"-{i} days", f"-{i} days", s))
    assert abs(db.avg_daily_sends_7d(kid7) - 1.0) < 0.01
    decisions = []
    for _ in range(4):
        d = make_draft(H7)
        r = c.post("/v1/sends", json={"draft_id": d}, headers=H7)
        decisions.append((r.json()["status"], r.json()["tier_decision"]))
    assert [s for s, _ in decisions] == ["SENT", "SENT", "SENT", "PENDING"], decisions
    assert decisions[3][1] == "anomaly", decisions
    print("7. anomaly rule -> PENDING ✔")

    # --- 8. webhook verifies signature + audits ---
    r = c.post("/v1/webhooks/stripe", content=b"{}",
               headers={"Stripe-Signature": "valid-sig"})
    assert r.json() == {"received": True, "type": "payment_intent.succeeded"}
    r = c.post("/v1/webhooks/stripe", content=b"{}",
               headers={"Stripe-Signature": "bad"})
    assert r.status_code == 400
    print("8. webhook verify + audit ✔")

    # --- 9. spend endpoint reflects tier/caps/spend ---
    r = c.get(f"/v1/admin/keys/{kid}/spend", headers=ADMIN).json()
    assert r["tier"] == "on_command" and r["has_payment_method"] is True
    assert r["spent_today_cents"] >= 234 and r["owner_email"] == "owner@example.com"
    r = c.post(f"/v1/admin/keys/{kid}/tier", json={"tier": "bogus"}, headers=ADMIN)
    assert r.status_code == 422
    print("9. spend endpoint + tier validation ✔")
finally:
    _exit(provider_patches)

os.remove(DB)
print("Billing/tiers/approval tests passed ✔")
