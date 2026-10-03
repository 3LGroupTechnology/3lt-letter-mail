"""Certified mail option: USPS Certified Mail with Electronic Return Receipt.

Covers:
  1. PostGrid payload carries mailingClass="certified_return_receipt" when
     certified=True, and omits mailingClass otherwise (first-class default).
  2. Pricing: certified letters price at PostGrid's certified rate +
     the unchanged flat $1.00 service fee, Stripe-grossed like normal mail.
  3. End-to-end: draft(certified=True) -> request_send -> approve passes
     certified=True through to the provider; default drafts stay first class.
  4. No API key: certified requests fail gracefully, never attempt a send.

No network, no keys, no charges — PostGrid is stubbed or never reached.
"""
import math
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
# Belt-and-suspenders: no provider key anywhere near this test run.
os.environ.pop("POSTGRID_API_KEY", None)
os.environ.pop("POSTGRID_AV_API_KEY", None)
os.environ.pop("ALLOW_LIVE_MAIL", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import patch  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import postgrid_client  # noqa: E402
import pricing  # noqa: E402
import tlt_billing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TO = {
    "name": "Jane Doe",
    "address_line1": "123 Main St",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}

# ---- 1. PostGrid payload ----------------------------------------------------
p = postgrid_client._letter_payload(TO, TO, "<p>hi</p>", False, None,
                                    certified=True)
assert p["mailingClass"] == "certified_return_receipt", p

p = postgrid_client._letter_payload(TO, TO, "<p>hi</p>", False, None)
assert "mailingClass" not in p, p  # default path unchanged: first class

p = postgrid_client._letter_payload(TO, TO, "<p>hi</p>", False, None,
                                    certified=False)
assert "mailingClass" not in p, p

# multipart flattening (PDF upload path) keeps the mailing class
flat = postgrid_client._flatten_for_multipart(
    postgrid_client._letter_payload(TO, TO, None, False, None, certified=True))
assert flat["mailingClass"] == "certified_return_receipt", flat

# ---- 2. pricing ---------------------------------------------------------------
# certified: PostGrid certified+return-receipt estimate + flat $1.00 fee
q = pricing.quote(0.97, 1, certified=True)
assert q["certified"] is True, q
assert q["base_cost_per_letter"] == 9.51, q
assert q["service_fee_per_letter"] == 1.00, q
assert q["estimated_total"] == 10.51, q

q = pricing.quote(0.97, 3, certified=True)
assert q["estimated_total"] == round(3 * 9.51 + 3.00, 2) == 31.53, q
assert q["service_fee_total"] == 3.00, q

# Stripe gross-up applies identically: net $10.51 -> ceil((1051+30)/0.971)
assert tlt_billing.gross_up(1051) == math.ceil(1081 / 0.971) == 1114

# non-certified pricing untouched
q = pricing.quote(0.97, 1)
assert q["certified"] is False and q["estimated_total"] == 1.97, q
q = pricing.quote(0.97, 10)
assert q["estimated_total"] == 19.70, q

# env override for the certified estimate
os.environ["POSTGRID_CERTIFIED_COST_USD"] = "5.00"
assert pricing.quote(0.97, 1, certified=True)["base_cost_per_letter"] == 5.00
del os.environ["POSTGRID_CERTIFIED_COST_USD"]

# ---- 3. end-to-end through the API --------------------------------------------
db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

r = c.post("/v1/keys", json={"name": "cert-agent"}, headers=ADMIN)
assert r.status_code == 200, r.text
H = {"X-API-Key": r.json()["api_key"]}

FAKE_VERIFICATION = {"deliverability": "deliverable", "components": {},
                     "raw": {}}
FAKE_LETTER = {"id": "ltr_cert1", "expected_delivery_date": "2026-10-09"}

with patch("mail_provider.verify_address",
           return_value=FAKE_VERIFICATION), \
     patch("mail_provider.is_test_mode", return_value=True), \
     patch("mail_provider.create_letter",
           return_value=FAKE_LETTER) as mock_create:
    # certified draft -> certified quote
    r = c.post("/v1/drafts",
               json={"to": TO, "from": TO, "html": "<p>Demand</p>",
                     "quantity": 1, "certified": True},
               headers=H)
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["certified"] is True
    assert draft["quote"]["certified"] is True
    assert draft["quote"]["base_cost_per_letter"] == 9.51
    assert draft["quote"]["estimated_total"] == 10.51
    assert draft["quote"]["service_fee_per_letter"] == 1.00
    assert draft["quote"]["total_charged"] == 11.14  # grossed-up 1051c
    cert_draft_id = draft["draft_id"]

    # default draft stays first class
    r = c.post("/v1/drafts",
               json={"to": TO, "from": TO, "html": "<p>Plain</p>"},
               headers=H)
    assert r.json()["certified"] is False
    assert r.json()["quote"]["estimated_total"] == 1.97
    plain_draft_id = r.json()["draft_id"]

    # certified PDF draft via the multipart form
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
           b"trailer<</Root 1 0 R>>\n%%EOF")
    import io as _io
    r = c.post("/v1/drafts/pdf",
               files={"file": ("letter.pdf", _io.BytesIO(pdf),
                               "application/pdf")},
               data={"to": '{"name":"Jane Doe","address_line1":"123 Main St",'
                           '"address_city":"Cleveland","address_state":"OH",'
                           '"address_zip":"44114"}',
                     "from": '{"name":"Acme","address_line1":"9 Mill Rd",'
                             '"address_city":"Cleveland","address_state":"OH",'
                             '"address_zip":"44114"}',
                     "certified": "true"},
               headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["certified"] is True
    assert r.json()["quote"]["estimated_total"] == 10.51

    # send the certified draft -> PENDING -> approve -> provider got certified
    r = c.post("/v1/sends", json={"draft_id": cert_draft_id}, headers=H)
    assert r.json()["status"] == "PENDING"
    req_id = r.json()["request_id"]
    r = c.post(f"/v1/admin/sends/{req_id}/approve", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SENT"
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["certified"] is True, \
        mock_create.call_args

    # plain draft approves with certified=False
    mock_create.reset_mock()
    r = c.post("/v1/sends", json={"draft_id": plain_draft_id}, headers=H)
    r = c.post(f"/v1/admin/sends/{r.json()['request_id']}/approve",
               headers=ADMIN)
    assert r.json()["status"] == "SENT"
    assert mock_create.call_args.kwargs["certified"] is False, \
        mock_create.call_args

# ---- 4. no key: graceful failure, nothing attempted ---------------------------
try:
    postgrid_client.create_letter(to=TO, from_address=TO, html="<p>x</p>",
                                  certified=True)
    raise AssertionError("expected PostGridError without an API key")
except postgrid_client.PostGridError as exc:
    assert "POSTGRID_API_KEY is not set" in str(exc), exc

try:
    postgrid_client.is_test_mode()
    raise AssertionError("expected PostGridError without an API key")
except postgrid_client.PostGridError:
    pass

os.remove(DB)
print("certified mail tests passed ✔ "
      "(payload, pricing, e2e certified=True/False, no-key failure)")
