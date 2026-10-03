"""Dynamic risk-engine tests: AI-set protective caps.

Covers: pure scoring (new key -> L0; 30d/25 clean -> L2; determinism),
key creation starts at L0 with L0 caps, content flag -> demote 1 level +
forced per-send approval for 7 days, chargeback -> frozen (sends 403),
velocity spike -> daily cap halved 24h, admin override sticks + is audited,
and the risk admin endpoints. No network; Stripe/PostGrid untouched here
(except where the API needs the stripe module importable).
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

DB = tempfile.mktemp(suffix=".db")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake123"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_fake123"
os.environ["TLT_MAILER_BACKEND"] = "log"
os.environ["POSTGRID_API_KEY"] = "test_fake_key"  # test mode; no real PostGrid calls
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
os.environ["APPROVAL_TOKEN_SECRET"] = "test-approval-secret"

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(os.path.dirname(SERVICE_DIR), "3lt-shared")
sys.path.insert(0, SERVICE_DIR)
sys.path.insert(0, SHARED_DIR)


# ---- fake stripe (importable; never meaningfully called here) ----------------
class _FakeStripeError(Exception):
    pass


class FakeStripe:
    error = type("error", (), {"StripeError": _FakeStripeError})


sys.modules["stripe"] = FakeStripe

import tlt_risk  # noqa: E402

# ---- 1. pure scoring ----------------------------------------------------------
r = tlt_risk.compute_risk({})
assert r["score"] == 50 and r["level"] == 0, r
assert any("base score 50" in x for x in r["reasons"])
print("1. new key -> score 50, level 0 ✔")

stats_l2 = {
    "tenure_days": 30, "clean_sends": 25, "flagged_sends": 0,
    "failed_payments": 0, "chargebacks": 0, "velocity_spikes": 0,
    "email_verified": True, "card_avs_cvc_pass": True,
    "card_type_risk": "low", "days_since_flag": None,
}
r = tlt_risk.compute_risk(stats_l2)
# 50 +5 email +10 avs +5 card +5 tenure7 +5 tenure30 +5 clean5 +5 clean25 = 90
assert r["score"] == 90, r
assert r["level"] == 2, r  # band L3, gate L2 -> L2
caps = tlt_risk.caps_for_level(2)
assert caps == {"monthly_cents": 50000, "daily_cents": 25000,
                "per_send_cents": 10000}, caps
print("2. 30d / 25 clean -> L2, $500/mo caps ✔")

# determinism: same inputs -> same outputs
a = tlt_risk.compute_risk(stats_l2)
b = tlt_risk.compute_risk(dict(stats_l2))
assert a == b
print("3. compute_risk deterministic ✔")

# level table sanity
assert tlt_risk.caps_for_level(0)["monthly_cents"] == 5000
assert tlt_risk.caps_for_level(1)["monthly_cents"] == 20000
assert tlt_risk.caps_for_level(3)["monthly_cents"] == 200000
try:
    tlt_risk.caps_for_level(9)
    raise AssertionError("expected ValueError")
except ValueError:
    pass
assert tlt_risk.demote_on_flag(0) == 0
assert tlt_risk.demote_on_flag(2) == 1
print("4. cap table + demote helper ✔")

# negative signals move the score
r = tlt_risk.compute_risk({**stats_l2, "flagged_sends": 1,
                           "days_since_flag": 0})
assert r["score"] == 65, r  # 90 - 25
r = tlt_risk.compute_risk({"chargebacks": 1})
assert r["score"] == 0, r  # 50 - 60 -> clamped
assert r["level"] == 0
print("5. negative signals (flag -25, chargeback clamp) ✔")

# ---- DB-backed integration -----------------------------------------------------
import api  # noqa: E402
import db  # noqa: E402
import risk  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

ADDR = {
    "name": "Jane Doe",
    "address_line1": "123 Main St",
    "address_city": "Cleveland",
    "address_state": "OH",
    "address_zip": "44114",
}


def issue_key(name="risk-key"):
    r = c.post("/v1/keys", json={"name": name}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], body["api_key"], body


def make_draft(key_header, html="<p>Hello</p>"):
    with patch("mail_provider.verify_address",
               return_value={"deliverability": "deliverable"}):
        r = c.post("/v1/drafts",
                   json={"to": ADDR, "from": ADDR, "html": html,
                         "quantity": 1},
                   headers=key_header)
    assert r.status_code == 200, r.text
    return r.json()["draft_id"]


def backdate_key(kid, days):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        conn.execute("UPDATE api_keys SET created_at = ? WHERE id = ?",
                     (ts, kid))


def add_clean_sends(kid, n):
    """Insert n SENT send requests directly (bypasses PostGrid/Stripe)."""
    quote = {"base_total": 0.97, "service_fee_total": 1.0,
             "estimated_total": 1.97}
    with db.connect() as conn:
        for _ in range(n):
            cur = conn.execute(
                """INSERT INTO drafts
                   (api_key_id, to_json, from_json, html, color, quantity,
                    description, quote_json)
                   VALUES (?, '{}', '{}', '<p>x</p>', 0, 1, '', ?)""",
                (kid, json.dumps(quote)))
            did = cur.lastrowid
            conn.execute(
                """INSERT INTO send_requests
                   (draft_id, status, amount_cents, decided_at)
                   VALUES (?, 'SENT', 197, datetime('now'))""", (did,))


# 6. admin-issued key starts at L0 with L0 caps
kid, raw, body = issue_key("fresh")
H = {"X-API-Key": raw}
row = db.get_key(kid)
assert row["risk_level"] == 0 and row["risk_score"] == 50, dict(row)
assert body["monthly_cap_cents"] == 5000, body
assert row["daily_cap_cents"] == 2500 and row["per_send_cap_cents"] == 5000
view = c.get(f"/v1/admin/keys/{kid}/risk", headers=ADMIN).json()
assert view["risk_level"] == 0 and view["risk_score"] == 50
assert view["effective_caps_cents"]["monthly_cents"] == 5000
assert view["frozen"] is False
assert isinstance(view["risk_reasons"], list) and view["risk_reasons"]
print("6. new key -> L0, $50/mo $25/day $25/send, risk endpoint ✔")

# 7. 30 days + 25 clean sends -> L2 automatically
kid2, raw2, _ = issue_key("aged")
backdate_key(kid2, 30)
add_clean_sends(kid2, 25)
res = risk.recompute(kid2, actor="test")
assert res["level"] == 2, res
# 50 +5 tenure7 +5 tenure30 +5 clean5 +5 clean25 = 70 -> band L2, gate L2
assert res["score"] == 70, res
row2 = db.get_key(kid2)
assert row2["monthly_cap_cents"] == 50000, dict(row2)
assert row2["daily_cap_cents"] == 25000
assert row2["per_send_cap_cents"] == 10000
print("7. 30d + 25 clean sends -> auto-promoted L2 ($500/mo) ✔")

# 8. content flag -> demote exactly 1 level + forced per-send approval 7d
res = risk.record_event(kid2, "flag", {"reason": "test flag"}, actor="test")
assert res["level"] == 1, res  # L2 -> L1
row2 = db.get_key(kid2)
until = datetime.fromisoformat(row2["force_approval_until"])
delta = until - datetime.now(timezone.utc)
assert timedelta(days=6) < delta <= timedelta(days=7, minutes=5), delta
assert risk.effective_require_approval(row2) is True
# audit trail carries the reasons
events = [e for e in db.recent_audit(50) if e["action"] == "risk.flag"]
assert events and "demoted_to" in (events[0]["detail"] or "")
print("8. content flag -> L2->L1 + per-send approval forced 7d ✔")

# 9. chargeback -> frozen; sends 403
kid3, raw3, _ = issue_key("cb")
H3 = {"X-API-Key": raw3}
did = make_draft(H3)
risk.record_event(kid3, "chargeback", {"reason": "test dispute"},
                  actor="test")
row3 = db.get_key(kid3)
frozen, reason = risk.check_frozen(row3)
assert frozen and "chargeback" in reason.lower()
r = c.post("/v1/sends", json={"draft_id": did}, headers=H3)
assert r.status_code == 403, (r.status_code, r.text)
assert "frozen" in r.json()["detail"].lower()
view3 = c.get(f"/v1/admin/keys/{kid3}/risk", headers=ADMIN).json()
assert view3["frozen"] is True and view3["risk_score"] < 50
print("9. chargeback -> frozen, sends 403 ✔")

# 10. velocity spike -> daily cap halved for 24h + audit alert
kid4, raw4, _ = issue_key("vel")
row4 = db.get_key(kid4)
assert risk.effective_caps(row4)["daily_cents"] == 2500
risk.record_event(kid4, "velocity_spike", {"sends_today": 40}, actor="test")
row4 = db.get_key(kid4)
assert risk.effective_caps(row4)["daily_cents"] == 1250, \
    risk.effective_caps(row4)
alerts = [e for e in db.recent_audit(50)
          if e["action"] == "risk.velocity_spike"]
assert alerts and "halved" in (alerts[0]["detail"] or "").lower()
print("10. velocity spike -> daily cap halved 24h + alert ✔")

# 11. admin override sticks and is audited
r = c.post(f"/v1/admin/keys/{kid4}/risk/override", json={"level": 3},
           headers=ADMIN)
assert r.status_code == 200, r.text
view = r.json()
assert view["effective_caps_cents"]["monthly_cents"] == 200000, view
assert view["override"] == {"level": 3}
# recompute does not clobber the pinned level's caps
risk.recompute(kid4, actor="test")
view = c.get(f"/v1/admin/keys/{kid4}/risk", headers=ADMIN).json()
assert view["effective_caps_cents"]["monthly_cents"] == 200000, view
aud = [e for e in db.recent_audit(50) if e["action"] == "risk.override"]
assert aud and "admin override" in (aud[0]["detail"] or "")
# clear -> back to risk-derived
r = c.post(f"/v1/admin/keys/{kid4}/risk/override", json={"clear": True},
           headers=ADMIN)
assert r.status_code == 200, r.text
assert r.json()["effective_caps_cents"]["monthly_cents"] == 5000, r.json()
print("11. admin override (level pin + clear) sticks, audited ✔")

# 12. admin unfreeze via override; custom caps override
r = c.post(f"/v1/admin/keys/{kid3}/risk/override",
           json={"frozen": False}, headers=ADMIN)
assert r.status_code == 200, r.text
assert r.json()["frozen"] is False
r = c.post(f"/v1/admin/keys/{kid}/risk/override",
           json={"caps": {"monthly_cap_cents": 9999}}, headers=ADMIN)
assert r.status_code == 200, r.text
assert r.json()["effective_caps_cents"]["monthly_cents"] == 9999, r.json()
assert r.json()["override"] == {"caps": {"monthly_cents": 9999}}, r.json()
print("12. unfreeze + custom caps override ✔")

# 13. risk event reporting endpoint (off-band signals)
r = c.post(f"/v1/admin/keys/{kid}/risk/event",
           json={"kind": "flag", "detail": {"reason": "manual report"}},
           headers=ADMIN)
assert r.status_code == 200, r.text
assert db.count_risk_events(kid, "flag") == 1
print("13. risk event endpoint ✔")

print("Risk engine tests passed ✔")
