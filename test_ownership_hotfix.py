"""Regression tests for the 2026-10-19 ownership/pdf_path hotfix.

Covers the three cross-tenant holes found on main:
  1. send_status leaked any customer's send request (incl. the one-tap
     approval_url while PENDING) to any valid API key — REST + MCP.
  2. request_send accepted any draft_id: a key could mail (and have
     billed to the draft owner) another key's draft.
  3. MCP pdf_path read any server-local file in draft_letter and
     create_fax_draft.

Expected post-fix behavior: cross-key access is a 404 (existence is not
disclosed); pdf_path is honored only inside the uploads directory;
same-key flows are unchanged. Stripe mocked; no network.
"""
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
RECEIPTS = tempfile.mkdtemp(prefix="tlt_hotfix_receipts_")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake123"
os.environ["TLT_MAILER_BACKEND"] = "log"
os.environ["TLT_RECEIPTS_DIR"] = RECEIPTS
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
os.environ["APPROVAL_TOKEN_SECRET"] = "test-approval-secret"
os.environ["LMS_UPLOADS_DIR"] = tempfile.mkdtemp(prefix="tlt_uploads_")

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(os.path.dirname(SERVICE_DIR), "3lt-shared")
sys.path.insert(0, SERVICE_DIR)
sys.path.insert(0, SHARED_DIR)


def make_pdf(text="Hello PDF World"):
    stream = f"BT /F1 24 Tf 100 700 Td ({text}) Tj ET".encode()
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(bodies) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n"
            f"{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


class _FakeStripeError(Exception):
    pass


class _FakeCustomer:
    @staticmethod
    def create(**kw):
        return {"id": "cus_fake123", "email": kw.get("email")}


class _FakePaymentIntent:
    @staticmethod
    def create(**kw):
        return {"id": "pi_hotfix1", "status": "requires_capture"}

    @staticmethod
    def capture(pid, **kw):
        return {"id": pid, "status": "succeeded"}

    @staticmethod
    def cancel(pid, **kw):
        return {"id": pid, "status": "canceled"}


class FakeStripe:
    Customer = _FakeCustomer
    PaymentIntent = _FakePaymentIntent
    error = type("error", (), {"StripeError": _FakeStripeError})
    api_key = "sk_test_fake123"


sys.modules["stripe"] = FakeStripe

from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import mcp_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}
FAKE_VERIFICATION = {"deliverability": "deliverable", "components": {}, "raw": {}}
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
    body = r.json()
    return body["id"], body["api_key"]


with patch("mail_provider.verify_address", return_value=FAKE_VERIFICATION), patch(
    "mail_provider.create_letter",
    return_value={"id": "ltr_hotfix", "expected_delivery_date": "2026-10-02"},
), patch("mail_provider.is_test_mode", return_value=True):
    kid_a, raw_a = issue_key("owner-a")
    kid_b, raw_b = issue_key("attacker-b")
    HA = {"X-API-Key": raw_a}
    HB = {"X-API-Key": raw_b}

    # A drafts a letter (approval tier by default).
    r = c.post("/v1/drafts",
               json={"to": ADDR, "from": ADDR, "html": "<p>private</p>",
                     "quantity": 1},
               headers=HA)
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft_id"]

    # 1. Hole 2: B cannot request_send A's draft (REST).
    r = c.post("/v1/sends", json={"draft_id": draft_id}, headers=HB)
    assert r.status_code == 404, (r.status_code, r.text)
    print("1. REST cross-key request_send -> 404 ✔")

    # 1b. Hole 2 via MCP: same rejection.
    res = mcp_server.request_send(api_key=raw_b, draft_id=draft_id)
    assert res.get("code") == 404, res
    print("1b. MCP cross-key request_send -> 404 ✔")

    # A requests its own send -> PENDING with an approval URL.
    r = c.post("/v1/sends", json={"draft_id": draft_id}, headers=HA)
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]

    # 2. Hole 1: B cannot read A's send status (REST), so no approval_url.
    r = c.get(f"/v1/sends/{request_id}", headers=HB)
    assert r.status_code == 404, (r.status_code, r.text)
    print("2. REST cross-key send_status -> 404 (no approval_url leak) ✔")

    # 2b. Hole 1 via MCP.
    res = mcp_server.send_status(api_key=raw_b, request_id=request_id)
    assert res.get("code") == 404, res
    print("2b. MCP cross-key send_status -> 404 ✔")

    # 2c. A reads its own status fine.
    r = c.get(f"/v1/sends/{request_id}", headers=HA)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING", r.json()
    res = mcp_server.send_status(api_key=raw_a, request_id=request_id)
    assert res.get("status") == "PENDING", res
    print("2c. same-key send_status works (REST + MCP) ✔")

    # 3. Hole 3: pdf_path outside the uploads dir is refused (MCP letter).
    outside = tempfile.mktemp(suffix=".pdf")  # /tmp — outside uploads
    with open(outside, "wb") as f:
        f.write(make_pdf("victim file"))
    res = mcp_server.draft_letter(api_key=raw_b, to=ADDR, from_address=ADDR,
                                  pdf_path=outside)
    assert res.get("code") == 422, res
    print("3. MCP draft_letter pdf_path outside uploads -> 422 ✔")

    # 3b. Same for fax drafts.
    res = mcp_server.create_fax_draft(api_key=raw_b,
                                      to_number="+12165550123",
                                      pdf_path=outside)
    assert res.get("code") == 422, res
    print("3b. MCP create_fax_draft pdf_path outside uploads -> 422 ✔")

    # 3c. A path INSIDE uploads still works.
    inside = os.path.join(os.environ["LMS_UPLOADS_DIR"], "ok.pdf")
    with open(inside, "wb") as f:
        f.write(make_pdf("inside uploads"))
    res = mcp_server.draft_letter(api_key=raw_a, to=ADDR, from_address=ADDR,
                                  pdf_path=inside)
    assert res.get("kind") == "pdf", res
    print("3c. MCP draft_letter pdf_path inside uploads still works ✔")

    os.remove(outside)

print("Ownership/pdf_path hotfix regression tests passed ✔")
