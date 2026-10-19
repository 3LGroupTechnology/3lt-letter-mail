"""Fax billing tests: fax wired into the letter billing model.

Covers, with Stripe fully mocked and Phaxio stubbed (no network):
  1. a fax send triggers EXACTLY ONE charge attempt at the quoted,
     grossed-up total, captures it, and writes a fax receipt record;
  2. a retry can never double-charge (409 + still one charge);
  3. a declined/insufficient payment leaves the fax UNSENT with a clear
     FAILED status, Phaxio never transmits, and the key is frozen
     (the failed_payment side effect, mirroring letters);
  4. risk caps block an over-limit fax BEFORE any charge or transmission
     (402, fax stays APPROVED) — including spend pooled from earlier faxes;
  5. a key with no payment method on file sends unbilled (audited skip),
     mirroring approval-tier letters;
  6. the billing_receipts migration rebuilds a pre-fax-billing table
     (send_request_id NOT NULL, no fax_id) without losing rows.
"""
import os
import subprocess
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
RECEIPTS = tempfile.mkdtemp(prefix="tlt_fax_receipts_")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["LMS_UPLOADS_DIR"] = tempfile.mkdtemp(prefix="tlt_faxb_uploads_")
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake123"
os.environ["TLT_MAILER_BACKEND"] = "log"
os.environ["TLT_RECEIPTS_DIR"] = RECEIPTS
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
os.environ["APPROVAL_TOKEN_SECRET"] = "test-approval-secret"
os.environ["PHAXIO_API_KEY"] = "test_phaxio_key"
os.environ["PHAXIO_API_SECRET"] = "test_phaxio_secret"
os.environ.pop("ALLOW_LIVE_FAX", None)

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(os.path.dirname(SERVICE_DIR), "3lt-shared")
sys.path.insert(0, SERVICE_DIR)
sys.path.insert(0, SHARED_DIR)


def make_pdf_pages(texts):
    """Build a minimal valid multi-page PDF with extractable text."""
    n = len(texts)
    font_obj = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    for i, text in enumerate(texts):
        contents_obj = 4 + 2 * i
        bodies.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {contents_obj} 0 R "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 24 Tf 100 700 Td ({text}) Tj ET".encode()
        bodies.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
    bodies.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    total = len(bodies) + 1
    out += f"xref\n0 {total}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n"
            f"{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


# ---- fake stripe -------------------------------------------------------------
class _FakeStripeError(Exception):
    pass


class _FakePaymentIntent:
    create_calls = []
    capture_calls = []
    cancel_calls = []
    mode = "ok"  # "ok" | "declined"

    @staticmethod
    def create(**kw):
        _FakePaymentIntent.create_calls.append(kw)
        if _FakePaymentIntent.mode == "declined":
            # Stripe's shape for an off-session decline: the PI comes back
            # unconfirmed instead of requires_capture.
            return {"id": "pi_declined", "status": "requires_payment_method"}
        return {"id": "pi_fake1", "status": "requires_capture"}

    @staticmethod
    def capture(pid, **kw):
        _FakePaymentIntent.capture_calls.append((pid, kw))
        return {"id": pid, "status": "succeeded"}

    @staticmethod
    def cancel(pid, **kw):
        _FakePaymentIntent.cancel_calls.append((pid, kw))
        return {"id": pid, "status": "canceled"}


class FakeStripe:
    PaymentIntent = _FakePaymentIntent
    error = type("error", (), {"StripeError": _FakeStripeError})
    api_key = None


sys.modules["stripe"] = FakeStripe

from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import phaxio_client  # noqa: E402
import risk  # noqa: E402
import tlt_billing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}
PDF1 = make_pdf_pages(["Hello Fax Billing"])

phaxio_calls = []


def fake_send_fax(**kw):
    phaxio_calls.append(kw)
    return {"id": "987654", "status": "queued", "num_pages": 1,
            "cost_cents": 7, "raw": {}}


def fake_get_fax(fax_id):
    return {"id": fax_id, "status": "success", "num_pages": 1,
            "cost_cents": 7, "raw": {}}


def issue_key(name, with_customer=True):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    if with_customer:
        db.set_key_stripe_customer(body["id"], "cus_fake123")
        db.set_key_owner_email(body["id"], "owner@example.com")
    return body["id"], body["api_key"]


def draft_and_approve(headers):
    r = c.post(
        "/v1/faxes",
        files={"file": ("fax.pdf", PDF1, "application/pdf")},
        data={"to_number": "2165550123"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    fax_id = r.json()["fax_id"]
    r = c.post(f"/v1/faxes/{fax_id}/approve", headers=headers)
    assert r.status_code == 200, r.text
    return fax_id


def stripe_counts():
    return (len(_FakePaymentIntent.create_calls),
            len(_FakePaymentIntent.capture_calls),
            len(_FakePaymentIntent.cancel_calls))


with patch("phaxio_client.send_fax", side_effect=fake_send_fax), patch(
    "phaxio_client.get_fax", side_effect=fake_get_fax
):
    # Gross for a 1-page fax: net $1.07 -> gross_up(107) = ceil(137/0.971).
    assert tlt_billing.gross_up(107) == 142

    # 1. exactly one charge attempt, at the quoted gross total --------------
    kid, key = issue_key("billed-agent")
    H = {"X-API-Key": key}
    fax_id = draft_and_approve(H)
    r = c.post(f"/v1/faxes/{fax_id}/send", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "SENT" and body["billed"] is True, body
    assert body["stripe_payment_id"] == "pi_fake1", body
    creates, captures, cancels = stripe_counts()
    assert (creates, captures, cancels) == (1, 1, 0), stripe_counts()
    auth = _FakePaymentIntent.create_calls[0]
    assert auth["amount"] == 142, auth  # the quoted total, grossed up
    assert auth["idempotency_key"] == f"fax_{fax_id}:auth", auth
    assert auth["metadata"] == {"fax_id": str(fax_id)}, auth
    cap_call = _FakePaymentIntent.capture_calls[0]
    assert cap_call[1].get("idempotency_key") == f"fax_{fax_id}:capture"
    assert len(phaxio_calls) == 1
    receipt = db.get_fax_receipt_by_idempotency(f"fax_{fax_id}:capture")
    assert receipt is not None and receipt["status"] == "succeeded"
    assert receipt["amount_cents"] == 142 and receipt["fax_id"] == fax_id
    assert receipt["send_request_id"] is None  # never aliased onto a letter
    st = c.get(f"/v1/faxes/{fax_id}", headers=H).json()
    assert st["billing"]["status"] == "succeeded", st
    assert st["billing"]["amount_cents"] == 142, st
    print("1. one authorize+capture at the $1.42 gross quote; fax receipt ✔")

    # 2. retry -> 409, still exactly one charge and one transmission -------
    r = c.post(f"/v1/faxes/{fax_id}/send", headers=H)
    assert r.status_code == 409, r.status_code
    assert stripe_counts() == (1, 1, 0), stripe_counts()
    assert len(phaxio_calls) == 1
    print("2. retry -> 409; no double charge, no retransmission ✔")

    # 3. declined payment -> fax UNSENT, clear FAILED status ---------------
    kid2, key2 = issue_key("declined-agent")
    H2 = {"X-API-Key": key2}
    fax2 = draft_and_approve(H2)
    _FakePaymentIntent.mode = "declined"
    try:
        r = c.post(f"/v1/faxes/{fax2}/send", headers=H2)
    finally:
        _FakePaymentIntent.mode = "ok"
    assert r.status_code == 502, (r.status_code, r.text)
    assert "Billing failed" in r.json()["detail"], r.text
    st = c.get(f"/v1/faxes/{fax2}", headers=H2).json()
    assert st["status"] == "FAILED", st
    assert st["failure_reason"].startswith("billing:"), st
    assert "authorization did not succeed" in st["failure_reason"], st
    assert len(phaxio_calls) == 1, "declined fax must never transmit"
    assert db.get_receipts_for_fax(fax2) == []
    # Mirror of letters: the failed payment froze the key.
    assert db.get_key(kid2)["risk_frozen"] == 1
    print("3. declined payment -> FAILED (billing: ...), Phaxio untouched, "
          "key frozen ✔")

    # 4a. caps block: monthly cap below one fax's gross --------------------
    kid3, key3 = issue_key("capped-agent")
    H3 = {"X-API-Key": key3}
    risk.apply_override(kid3, {"caps": {"monthly_cents": 100,
                                        "daily_cents": 2500,
                                        "per_send_cents": 5000}},
                        actor="test")
    fax3 = draft_and_approve(H3)
    before = stripe_counts()
    r = c.post(f"/v1/faxes/{fax3}/send", headers=H3)
    assert r.status_code == 402, (r.status_code, r.text)
    assert "Monthly spend cap" in r.json()["detail"], r.text
    assert stripe_counts() == before, "cap block must precede any charge"
    assert len(phaxio_calls) == 1, "cap block must precede transmission"
    st = c.get(f"/v1/faxes/{fax3}", headers=H3).json()
    assert st["status"] == "APPROVED", st  # retryable once the cap allows
    print("4a. monthly cap < fax price -> 402 before charge/transmit ✔")

    # 4b. caps pool across sends: agent 1 already spent 142 this month;
    # a 200 monthly cap leaves no room for a second 142 fax.
    risk.apply_override(kid, {"caps": {"monthly_cents": 200,
                                       "daily_cents": 2500,
                                       "per_send_cents": 5000}},
                        actor="test")
    fax4 = draft_and_approve(H)
    r = c.post(f"/v1/faxes/{fax4}/send", headers=H)
    assert r.status_code == 402, (r.status_code, r.text)
    assert stripe_counts() == (2, 1, 0), stripe_counts()  # only the decline
    # attempt from test 3 added a create; no capture, no new charge here
    print("4b. prior fax spend pools into the monthly cap -> 402 ✔")

    # 5. no payment method on file -> sends unbilled, like letters --------
    kid5, key5 = issue_key("cashless-agent", with_customer=False)
    H5 = {"X-API-Key": key5}
    fax5 = draft_and_approve(H5)
    before = stripe_counts()
    r = c.post(f"/v1/faxes/{fax5}/send", headers=H5)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SENT" and r.json()["billed"] is False
    assert stripe_counts() == before, "no customer -> no Stripe calls"
    assert db.get_receipts_for_fax(fax5) == []
    print("5. no payment method -> SENT unbilled (audited skip) ✔")

# 6. migration: a pre-fax-billing receipts table is rebuilt, rows kept ----
legacy = tempfile.mktemp(suffix=".db")
import sqlite3  # noqa: E402

conn = sqlite3.connect(legacy)
conn.executescript(
    """
    CREATE TABLE billing_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        send_request_id INTEGER NOT NULL,
        api_key_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'usd',
        stripe_payment_id TEXT,
        status TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    INSERT INTO billing_receipts
        (send_request_id, api_key_id, amount_cents, stripe_payment_id,
         status, idempotency_key)
    VALUES (7, 1, 246, 'pi_legacy', 'succeeded', 'sendreq_7:capture');
    """
)
conn.commit()
conn.close()
env = dict(os.environ, LMS_DB_PATH=legacy)
out = subprocess.run(
    [sys.executable, "-c",
     "import db; db.init_db();"
     "import sqlite3, os;"
     "conn = sqlite3.connect(os.environ['LMS_DB_PATH']);"
     "conn.row_factory = sqlite3.Row;"
     "cols = [r['name'] for r in conn.execute("
     "'PRAGMA table_info(billing_receipts)')];"
     "row = conn.execute('SELECT * FROM billing_receipts').fetchone();"
     "assert 'fax_id' in cols, cols;"
     "assert row['send_request_id'] == 7 and row['fax_id'] is None, dict(row);"
     "print('legacy receipt preserved')"],
    cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
)
assert out.returncode == 0, (out.stdout, out.stderr)
os.remove(legacy)
print("6. pre-fax billing_receipts rebuilt; legacy rows preserved ✔")

os.remove(DB)
print("Fax billing tests passed ✔ (charge-once, decline, caps, migration)")
