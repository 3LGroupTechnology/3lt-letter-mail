"""PDF draft upload tests: multipart upload, magic-byte/size validation,
text extraction for screening, needs_review fallback, PostGrid PDF passthrough,
and the MCP draft_letter pdf_path option. Stripe mocked; no network.
"""
import io
import json
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
RECEIPTS = tempfile.mkdtemp(prefix="tlt_pdf_receipts_")
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
    """Build a minimal valid one-page PDF with extractable text."""
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
    create_calls = []

    @staticmethod
    def create(**kw):
        _FakePaymentIntent.create_calls.append(kw)
        return {"id": "pi_pdf1", "status": "requires_capture"}

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
    api_key = None


sys.modules["stripe"] = FakeStripe

from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402
import mcp_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

FAKE_VERIFICATION = {"deliverability": "deliverable", "components": {}, "raw": {}}
FAKE_LETTER = {"id": "ltr_pdffake", "expected_delivery_date": "2026-10-02"}
ADDR = {
    "name": "Jane Doe",
    "address_line1": "123 Main St",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}

provider_calls = []


def fake_create_letter(**kw):
    provider_calls.append(kw)
    return FAKE_LETTER


def upload_pdf(key_header, pdf_bytes, filename="letter.pdf", extra=None):
    data = {"to": json.dumps(ADDR), "from": json.dumps(ADDR)}
    if extra:
        data.update(extra)
    return c.post(
        "/v1/drafts/pdf",
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data=data,
        headers=key_header,
    )


def issue_key(name="pdf-agent"):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], body["api_key"]


with patch("mail_provider.verify_address", return_value=FAKE_VERIFICATION), patch(
    "mail_provider.create_letter", side_effect=fake_create_letter
), patch("mail_provider.is_test_mode", return_value=True):
    kid, raw = issue_key()
    H = {"X-API-Key": raw}
    c.post(f"/v1/admin/keys/{kid}/email",
           json={"owner_email": "pdfowner@example.com"}, headers=ADMIN)
    c.post(f"/v1/admin/keys/{kid}/stripe-customer", json={}, headers=ADMIN)

    # 1. valid PDF upload -> draft with page count, then full flow to SENT
    pdf = make_pdf()
    r = upload_pdf(H, pdf)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "pdf" and d["page_count"] == 1
    assert d["needs_review"] is False
    assert d["quote"]["estimated_total"] == 1.97  # same pricing as HTML
    draft_id = d["draft_id"]
    # stored under a per-draft directory
    row = db.get_draft(draft_id)
    assert row["pdf_path"] and os.path.isfile(row["pdf_path"])
    assert f"draft_{draft_id}" in row["pdf_path"]

    r = c.post("/v1/sends", json={"draft_id": draft_id}, headers=H)
    assert r.json()["status"] == "PENDING", r.text
    req_id = r.json()["request_id"]
    r = c.post(f"/v1/admin/sends/{req_id}/approve", headers=ADMIN)
    body = r.json()
    assert body["status"] == "SENT", body
    assert body["stripe_payment_id"] == "pi_pdf1"
    # PostGrid received the PDF file path (native PDF passthrough)
    assert provider_calls and provider_calls[-1]["pdf_path"] == row["pdf_path"]
    assert provider_calls[-1]["html"] is None
    # receipt row + emailed receipt reference the charge
    receipts = db.get_receipts_for_request(req_id)
    assert receipts and receipts[0]["stripe_payment_id"] == "pi_pdf1"
    print("1. PDF upload -> PENDING -> SENT with Stripe charge + PostGrid PDF ✔")

    # 2. non-PDF upload rejected
    r = upload_pdf(H, b"this is not a pdf at all")
    assert r.status_code == 422, r.status_code
    print("2. non-PDF rejected with 422 ✔")

    # 3. oversize rejected
    with patch("core.MAX_PDF_BYTES", 100):
        r = upload_pdf(H, make_pdf())
    assert r.status_code == 413, r.status_code
    print("3. oversize PDF rejected with 413 ✔")

    # 4. unextractable PDF -> kept but needs_review; never auto-sends
    kid2, raw2 = issue_key("pdf-auto")
    H2 = {"X-API-Key": raw2}
    c.post(f"/v1/admin/keys/{kid2}/tier", json={"tier": "on_command"}, headers=ADMIN)
    # rail default for admin keys is per-send approval ON — opt out so the
    # needs_review guardrail (not the approval flag) is what holds this send
    c.post(f"/v1/admin/keys/{kid2}/per-send-approval",
           json={"require": False}, headers=ADMIN)
    c.post(f"/v1/admin/keys/{kid2}/stripe-customer", json={}, headers=ADMIN)
    garbage = b"%PDF-1.4\nthis body is garbage and no parser will read it"
    r = upload_pdf(H2, garbage)
    assert r.status_code == 200, r.text
    assert r.json()["needs_review"] is True
    r = c.post("/v1/sends", json={"draft_id": r.json()["draft_id"]}, headers=H2)
    body = r.json()
    assert body["status"] == "PENDING", body
    assert body["tier_decision"] == "needs_review", body
    print("4. unextractable PDF -> needs_review -> PENDING, never auto-sends ✔")

    # 5. blocked phrase inside PDF text -> 422 at draft
    r = upload_pdf(H, make_pdf("send bitcoin now"))
    assert r.status_code == 422, r.status_code
    print("5. PDF text screened at draft (blocked phrase -> 422) ✔")

    # 6. MCP draft_letter accepts pdf_path
    tmp = tempfile.mktemp(suffix=".pdf")
    with open(tmp, "wb") as f:
        f.write(make_pdf("via mcp"))
    res = mcp_server.draft_letter(api_key=raw, to=ADDR, from_address=ADDR,
                                  pdf_path=tmp)
    assert res.get("kind") == "pdf", res
    assert res["page_count"] == 1
    res = mcp_server.draft_letter(api_key=raw, to=ADDR, from_address=ADDR,
                                  pdf_path="/nonexistent/x.pdf")
    assert res["code"] == 422, res
    os.remove(tmp)
    print("6. MCP draft_letter pdf_path ✔")

os.remove(DB)
print("PDF draft tests passed ✔")
