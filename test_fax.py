"""Fax tests: Phaxio-backed fax lifecycle mirroring the letter rail.

Covers: draft (number validation + page count + screening + quote) ->
approve -> send happy path with Phaxio stubbed; send-before-approve and
double-send guards; bad-number rejection; screening block; per-key
ownership scoping; the ALLOW_LIVE_FAX live guard (core + client, refusal
and allowed paths); and the MCP fax tools. No network.
"""
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["LMS_UPLOADS_DIR"] = tempfile.mkdtemp(prefix="tlt_fax_uploads_")
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


from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import mcp_server  # noqa: E402
import phaxio_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

send_calls = []


def fake_send_fax(**kw):
    send_calls.append(kw)
    return {"id": "987654", "status": "queued", "num_pages": 1,
            "cost_cents": 7, "raw": {}}


def fake_get_fax(fax_id):
    return {"id": fax_id, "status": "success", "num_pages": 1,
            "cost_cents": 7, "raw": {}}


# Captured before stubbing: the client-level guard test must exercise the
# REAL phaxio_client.send_fax (its _guard_send), not the stub.
_real_send_fax = phaxio_client.send_fax


def issue_key(name="fax-agent"):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


KEY = issue_key()
H = {"X-API-Key": KEY}
PDF1 = make_pdf_pages(["Hello Fax World"])
PDF3 = make_pdf_pages(["Page one", "Page two", "Page three"])


def post_fax(pdf_bytes, to_number, headers=H):
    return c.post(
        "/v1/faxes",
        files={"file": ("fax.pdf", pdf_bytes, "application/pdf")},
        data={"to_number": to_number},
        headers=headers,
    )


with patch("phaxio_client.send_fax", side_effect=fake_send_fax), patch(
    "phaxio_client.get_fax", side_effect=fake_get_fax
):
    # 1. happy path: draft -> approve -> send, quote math, normalization
    r = post_fax(PDF1, "(216) 555-0123")
    assert r.status_code == 200, r.text
    fax = r.json()
    assert fax["status"] == "DRAFT"
    assert fax["to_number"] == "+12165550123", fax  # normalized to E.164
    assert fax["page_count"] == 1
    assert fax["needs_review"] is False
    assert fax["test_mode"] is True
    q = fax["quote"]
    assert q["pages"] == 1 and q["base_cost_per_page"] == 0.07
    assert q["base_total"] == 0.07 and q["service_fee_total"] == 1.00
    assert q["estimated_total"] == 1.07, q
    fax_id = fax["fax_id"]
    print("1. fax draft -> normalized number, 1 page, $0.07 + $1.00 = $1.07 ✔")

    # 2. send before approve -> 409, nothing transmitted
    r = c.post(f"/v1/faxes/{fax_id}/send", headers=H)
    assert r.status_code == 409, (r.status_code, r.text)
    assert send_calls == []
    print("2. send before approve -> 409, Phaxio untouched ✔")

    # 3. approve -> APPROVED; double approve -> 409
    r = c.post(f"/v1/faxes/{fax_id}/approve", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"
    assert r.json()["approved_by"].startswith("key:")
    r = c.post(f"/v1/faxes/{fax_id}/approve", headers=H)
    assert r.status_code == 409, r.status_code
    print("3. approve records the gate; double approve -> 409 ✔")

    # 4. send -> SENT via stub, stable idempotency key; double send -> 409
    r = c.post(f"/v1/faxes/{fax_id}/send", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "SENT" and body["provider_fax_id"] == "987654"
    assert len(send_calls) == 1
    call = send_calls[0]
    assert call["to"] == "+12165550123"
    assert call["idempotency_key"] == f"3lt-fax-{fax_id}", call
    assert call["pdf_path"].endswith(f"fax_{fax_id}/source.pdf")
    r = c.post(f"/v1/faxes/{fax_id}/send", headers=H)
    assert r.status_code == 409, r.status_code
    assert len(send_calls) == 1, "double send must never retransmit"
    print("4. send -> SENT once, idempotency key 3lt-fax-<id>; retry -> 409 ✔")

    # 5. status readback includes live provider status
    r = c.get(f"/v1/faxes/{fax_id}", headers=H)
    st = r.json()
    assert st["status"] == "SENT" and st["provider_fax_id"] == "987654"
    assert st["provider_status"] == "success", st
    assert st["provider_cost_cents"] == 7
    print("5. GET fax -> SENT + Phaxio delivery status refreshed ✔")

    # 6. multi-page quote: 3 pages x $0.07 + $1.00 flat = $1.21
    r = post_fax(PDF3, "+1 216 555 0123")
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    assert r.json()["page_count"] == 3
    assert q["base_total"] == 0.21 and q["estimated_total"] == 1.21, q
    print("6. 3-page fax quote -> $0.21 base + $1.00 fee = $1.21 ✔")

    # 7. bad numbers -> 422 (too short, 7-digit, letters, short E.164)
    for bad in ("123", "555-0123", "call me maybe", "+12345", ""):
        r = post_fax(PDF1, bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    print("7. bad fax numbers -> 422 ✔")

    # 8. full international number accepted as-is
    r = post_fax(PDF1, "+44 20 7946 0958")
    assert r.status_code == 200, r.text
    assert r.json()["to_number"] == "+442079460958"
    print("8. E.164 international number accepted ✔")

    # 9. screening blocks a fax draft before anything is stored
    def fax_count():
        with db.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM faxes").fetchone()["n"]

    before = fax_count()
    r = post_fax(make_pdf_pages(["please send bitcoin now"]), "2165550123")
    assert r.status_code == 422, (r.status_code, r.text)
    assert fax_count() == before, "blocked fax must not be stored"
    print("9. screening block -> 422, no fax row stored ✔")

    # 10. ownership: another key cannot see/approve/send this key's fax
    other = issue_key("other-agent")
    H2 = {"X-API-Key": other}
    assert c.get(f"/v1/faxes/{fax_id}", headers=H2).status_code == 404
    assert c.post(f"/v1/faxes/{fax_id}/approve", headers=H2).status_code == 404
    assert c.post(f"/v1/faxes/{fax_id}/send", headers=H2).status_code == 404
    print("10. foreign key -> 404 on read/approve/send ✔")

    # 11. auth enforcement
    assert c.post("/v1/faxes", files={"file": ("f.pdf", PDF1,
              "application/pdf")}, data={"to_number": "2165550123"}
              ).status_code == 401
    assert c.get(f"/v1/faxes/{fax_id}",
                 headers={"X-API-Key": "lms_bogus"}).status_code == 403
    print("11. missing key -> 401, bogus key -> 403 ✔")

    # 12. live guard: live key + no ALLOW_LIVE_FAX -> 403, fax stays APPROVED
    r = post_fax(PDF1, "2165550123")
    live_fax_id = r.json()["fax_id"]
    r = c.post(f"/v1/faxes/{live_fax_id}/approve", headers=H)
    assert r.status_code == 200, r.text
    calls_before = len(send_calls)
    os.environ["PHAXIO_API_KEY"] = "live_phaxio_key"
    try:
        r = c.post(f"/v1/faxes/{live_fax_id}/send", headers=H)
        assert r.status_code == 403, (r.status_code, r.text)
        assert "ALLOW_LIVE_FAX" in r.json()["detail"]
        assert len(send_calls) == calls_before, "live fax must not transmit"
        st = c.get(f"/v1/faxes/{live_fax_id}", headers=H).json()
        assert st["status"] == "APPROVED", st
        # client-level guards trip too (boot guard + send-time guard)
        try:
            phaxio_client.guard_live_key()
            raise AssertionError("guard_live_key should refuse a live key")
        except RuntimeError as exc:
            assert "ALLOW_LIVE_FAX" in str(exc)
        try:
            _real_send_fax(to="+12165550123", pdf_path="x.pdf")
            raise AssertionError("send_fax should refuse a live key")
        except phaxio_client.PhaxioError as exc:
            assert "ALLOW_LIVE_FAX" in str(exc)
        # ... and both open when the deployment explicitly allows live fax
        os.environ["ALLOW_LIVE_FAX"] = "1"
        phaxio_client.guard_live_key()  # must not raise
        r = c.post(f"/v1/faxes/{live_fax_id}/send", headers=H)
        assert r.status_code == 200 and r.json()["status"] == "SENT", r.text
        assert len(send_calls) == calls_before + 1  # stub "transmitted"
    finally:
        os.environ["PHAXIO_API_KEY"] = "test_phaxio_key"
        os.environ.pop("ALLOW_LIVE_FAX", None)
    print("12. live key refused without ALLOW_LIVE_FAX=1; allowed with it ✔")

    # 14. MCP tools: draft -> approve -> send -> status, same core flow
    tmp_pdf = os.path.join(os.environ["LMS_UPLOADS_DIR"], "mcp_doc.pdf")
    with open(tmp_pdf, "wb") as f:
        f.write(PDF1)
    out = mcp_server.create_fax_draft(KEY, "216-555-0123", tmp_pdf)
    assert out["status"] == "DRAFT" and out["to_number"] == "+12165550123", out
    mcp_fax_id = out["fax_id"]
    out = mcp_server.send_fax(KEY, mcp_fax_id)
    assert out["code"] == 409, out  # unapproved cannot send, even via MCP
    out = mcp_server.approve_fax(KEY, mcp_fax_id)
    assert out["status"] == "APPROVED", out
    out = mcp_server.send_fax(KEY, mcp_fax_id)
    assert out["status"] == "SENT" and out["provider_fax_id"] == "987654", out
    out = mcp_server.get_fax_status(KEY, mcp_fax_id)
    assert out["status"] == "SENT" and out["provider_status"] == "success", out
    out = mcp_server.create_fax_draft(KEY, "123", tmp_pdf)
    assert out["code"] == 422 and "error" in out, out
    out = mcp_server.create_fax_draft(KEY, "2165550123", "/nonexistent.pdf")
    assert out["code"] == 422, out
    print("14. MCP fax tools mirror the REST lifecycle ✔")

# 15. unconfigured fax (REAL client, no stubs): draft still works, the
# send fails honestly at the provider boundary -> 502 + FAILED.
saved_key = os.environ.pop("PHAXIO_API_KEY")
saved_secret = os.environ.pop("PHAXIO_API_SECRET")
try:
    r = post_fax(PDF1, "2165550123")
    assert r.status_code == 200 and r.json()["test_mode"] is None
    fid = r.json()["fax_id"]
    assert c.post(f"/v1/faxes/{fid}/approve", headers=H).status_code == 200
    r = c.post(f"/v1/faxes/{fid}/send", headers=H)
    assert r.status_code == 502, (r.status_code, r.text)
    st = c.get(f"/v1/faxes/{fid}", headers=H).json()
    assert st["status"] == "FAILED" and "PHAXIO_API_KEY" in st["failure_reason"]
finally:
    os.environ["PHAXIO_API_KEY"] = saved_key
    os.environ["PHAXIO_API_SECRET"] = saved_secret
print("15. Phaxio unconfigured -> draft ok, send 502 + FAILED (never faked) ✔")

os.remove(DB)
print("Fax tests passed ✔ (draft -> approve -> send, guards, screening, MCP)")
