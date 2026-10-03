"""Customer dashboard tests: key-scoped self-service endpoints.

Covers: GET /v1/sends and GET /v1/account auth enforcement (401 with no key,
403 with a bad key), per-key isolation (one key can never see another key's
sends or account), and GET /customer serving the dashboard page.
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


def issue_key(name):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


ADDR_A = {
    "name": "Alice Adams",
    "address_line1": "1 Apple Way",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}
ADDR_B = {
    "name": "Bob Baker",
    "address_line1": "2 Birch Rd",
    "address_city": "Columbus",
    "address_state": "OH",
    "address_zip": "43215",
}

KEY_A = issue_key("customer-a")
KEY_B = issue_key("customer-b")
HA = {"X-API-Key": KEY_A}
HB = {"X-API-Key": KEY_B}
BAD = {"X-API-Key": "lms_thisisnotarealkey0000000000000000"}

with patch("mail_provider.verify_address", return_value=FAKE_VERIFICATION), patch(
    "mail_provider.is_test_mode", return_value=True
):
    # One draft + send request belonging to key A only.
    r = c.post(
        "/v1/drafts",
        json={"to": ADDR_A, "from": ADDR_A, "html": "<p>Hello Alice</p>",
              "quantity": 2},
        headers=HA,
    )
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft_id"]
    r = c.post("/v1/sends", json={"draft_id": draft_id}, headers=HA)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING"
    REQ_A = r.json()["request_id"]

# ---- /v1/sends auth -------------------------------------------------------
r = c.get("/v1/sends")
assert r.status_code == 401, r.text

r = c.get("/v1/sends", headers=BAD)
assert r.status_code == 403, r.text

# ---- /v1/sends isolation ---------------------------------------------------
# Key B sees nothing — key A's send must not leak.
r = c.get("/v1/sends", headers=HB)
assert r.status_code == 200, r.text
assert r.json()["sends"] == [], r.text

# Key A sees exactly its own send, with the expected shape.
r = c.get("/v1/sends", headers=HA)
assert r.status_code == 200, r.text
sends = r.json()["sends"]
assert len(sends) == 1, r.text
s = sends[0]
assert s["request_id"] == REQ_A
assert s["status"] == "PENDING"
assert s["recipient_name"] == "Alice Adams"
assert s["recipient_address"]["line1"] == "1 Apple Way"
assert s["recipient_address"]["city"] == "Cleveland"
assert s["quantity"] == 2
assert s["certified"] is False
assert "Alice Adams" not in r.text or True  # own data is fine
assert "Bob Baker" not in r.text  # nothing of B's (or anyone else's)

# Status filter narrows within the caller's own sends.
r = c.get("/v1/sends?status=PENDING", headers=HA)
assert len(r.json()["sends"]) == 1, r.text
r = c.get("/v1/sends?status=SENT", headers=HA)
assert r.json()["sends"] == [], r.text

# ---- /v1/account auth -------------------------------------------------------
r = c.get("/v1/account")
assert r.status_code == 401, r.text

r = c.get("/v1/account", headers=BAD)
assert r.status_code == 403, r.text

# ---- /v1/account isolation + shape -------------------------------------------
r = c.get("/v1/account", headers=HA)
assert r.status_code == 200, r.text
a = r.json()
assert a["name"] == "customer-a"
assert a["key_prefix"].startswith("lms_")
assert a["tier"] == "approval"
assert a["require_per_send_approval"] is True
assert isinstance(a["monthly_cap_cents"], int) and a["monthly_cap_cents"] > 0
assert a["spent_month_cents"] == 0
assert a["risk_level"] == 0
assert a["frozen"] is False
assert "customer-b" not in r.text  # no trace of the other key

r = c.get("/v1/account", headers=HB)
assert r.status_code == 200, r.text
assert r.json()["name"] == "customer-b"
assert "customer-a" not in r.text

# ---- /customer page -----------------------------------------------------------
r = c.get("/customer")
assert r.status_code == 200, r.text
assert "text/html" in r.headers["content-type"]
assert "Customer Dashboard" in r.text
# Page is static: it must not ship anything resembling a real issued key
# (lms_ + 32+ token chars). The "lms_" prefix hint in the placeholder is fine.
import re

assert not re.search(r"lms_[A-Za-z0-9_-]{32,}", r.text), "key material in page"

print("customer dashboard tests: all assertions passed")
