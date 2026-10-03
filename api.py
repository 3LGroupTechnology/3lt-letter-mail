"""FastAPI REST layer + approval-dashboard backend.

Auth:
  - Agent endpoints: X-API-Key header (keys issued via POST /v1/keys).
  - Admin endpoints (approve/reject/issue keys): X-Admin-Token header, from the
    LMS_ADMIN_TOKEN env var. If unset, a random token is generated and printed
    once at startup (dev convenience only — set the env var in production).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

# Shared 3L-Group Technology modules live in ~/workspace/3lt-shared.
_SHARED = os.environ.get("TLT_SHARED_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "3lt-shared"
)
if os.path.isdir(_SHARED) and _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import core
import db
import mail_provider
import risk
import tlt_approvals
import tlt_billing
import tlt_tiers

ADMIN_TOKEN = os.environ.get("LMS_ADMIN_TOKEN")
if not ADMIN_TOKEN:
    ADMIN_TOKEN = secrets.token_urlsafe(24)
    print(
        "[lms] LMS_ADMIN_TOKEN not set; generated one-time admin token:\n"
        f"[lms]   {ADMIN_TOKEN}\n"
        "[lms] Set LMS_ADMIN_TOKEN in production.",
        flush=True,
    )

app = FastAPI(title="3L-Group Technology — Letter Mail API", version="0.1.0")
db.init_db()
mail_provider.guard_live_key()

HERE = os.path.dirname(os.path.abspath(__file__))


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def require_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    if not x_api_key:
        raise HTTPException(401, "Missing X-API-Key header")
    row = db.get_key_by_hash(_hash(x_api_key))
    if not row:
        raise HTTPException(403, "Invalid or revoked API key")
    return row


def require_admin(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(403, "Invalid admin token")


def _err(exc: core.AppError) -> HTTPException:
    return HTTPException(exc.code, str(exc))


# ---- models ---------------------------------------------------------------
class CreateKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AddressBody(BaseModel):
    name: str
    address_line1: str
    address_line2: Optional[str] = None
    address_city: str
    address_state: str
    address_zip: str
    address_country: str = "US"


class DraftBody(BaseModel):
    to: AddressBody
    from_address: AddressBody = Field(alias="from")
    html: str = Field(min_length=1, max_length=200_000)
    color: bool = False
    quantity: int = Field(default=1, ge=1, le=1000)
    description: Optional[str] = Field(default=None, max_length=200)
    certified: bool = Field(
        default=False,
        description="USPS Certified Mail with Electronic Return Receipt "
                    "(tracked, signature on delivery). Priced at PostGrid's "
                    "certified rate + the flat $1.00 service fee.",
    )

    class Config:
        populate_by_name = True


class SendBody(BaseModel):
    draft_id: int


class RejectBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class TierBody(BaseModel):
    tier: str = Field(pattern="^(approval|on_command)$")


class CapsBody(BaseModel):
    daily_cap_cents: Optional[int] = Field(default=None, ge=0, le=100_000_00)
    per_send_cap_cents: Optional[int] = Field(default=None, ge=0, le=100_000_00)
    monthly_cap_cents: Optional[int] = Field(default=None, ge=0, le=100_000_00)


class PerSendApprovalBody(BaseModel):
    require: bool


class ConnectBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner_email: str = Field(min_length=3, max_length=254)
    monthly_cap_cents: Optional[int] = Field(default=20000, ge=1000,
                                             le=100_000_00)


class ConnectFinalizeBody(BaseModel):
    setup_intent_id: str = Field(min_length=1, max_length=120)


class EmailBody(BaseModel):
    owner_email: str = Field(min_length=3, max_length=254)


class StripeCustomerBody(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[str] = Field(default=None, max_length=254)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_email(email: str) -> str:
    email = (email or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "invalid email address")
    return email


# ---- public ----------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "3l-group-technology-letter-mail"}


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(HERE, "dashboard", "index.html"))


# ---- admin: api keys --------------------------------------------------------
@app.post("/v1/keys")
def create_key(body: CreateKeyBody, _=Depends(require_admin)):
    raw = "lms_" + secrets.token_urlsafe(32)
    key_id = db.create_key(body.name, _hash(raw), raw[:12],
                           require_per_send_approval=True)
    # Risk engine owns the caps from birth: every new key starts at L0.
    caps = risk.initialize_key(key_id, actor="admin")["caps"]
    db.audit("admin", "key.create", "api_key", key_id, {"name": body.name})
    return {
        "id": key_id,
        "name": body.name,
        "api_key": raw,
        "tier": "approval",
        "require_per_send_approval": True,
        "monthly_cap_cents": caps["monthly_cents"],
        "risk_level": 0,
        "warning": "Store this key now. It is never shown again.",
    }


# ---- one-time rail authorization ("Connect") ---------------------------------
# The end customer never touches 3LT directly: their AI assistant (the key
# holder) completes this flow once — Stripe customer + saved payment method +
# monthly cap — and receives an on_command API key with per-send approval
# OFF. That single authorization is the standing approval for all future
# sends inside the caps. No per-send prompts, ever (unless opted in later).
CONNECT_TTL_SECONDS = 3600


def _connect_session_or_404(token: str):
    now = datetime.now(timezone.utc).isoformat()
    row = db.get_connect_session(_hash(token), now)
    if not row:
        raise HTTPException(404, "connect session not found or expired")
    return row


@app.post("/v1/connect")
def connect_start(body: ConnectBody):
    """Start the one-time rail authorization. Public endpoint — this is how
    an assistant platform / developer connects: we create a Stripe customer
    (test mode) and hand back a /connect/<token> page where the key holder
    saves a payment method and confirms the monthly cap."""
    email = _check_email(body.owner_email)
    try:
        customer = tlt_billing.create_customer(
            name=body.name, email=email, metadata={"flow": "connect"})
    except tlt_billing.BillingError as exc:
        raise HTTPException(502, str(exc))
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc)
                  + timedelta(seconds=CONNECT_TTL_SECONDS)).isoformat()
    session_id = db.create_connect_session(
        _hash(token), body.name, email, body.monthly_cap_cents,
        customer["id"], expires_at)
    db.audit("connect", "connect.started", "connect_session", session_id,
             {"name": body.name, "owner_email": email,
              "monthly_cap_cents": body.monthly_cap_cents,
              "stripe_customer_id": customer["id"]})
    return {
        "connect_url": f"{core.PUBLIC_BASE_URL}/connect/{token}",
        "expires_at": expires_at,
        "monthly_cap_cents": body.monthly_cap_cents,
        "message": "Open the connect_url, save a payment method, then finalize "
                   "to receive your API key.",
    }


@app.get("/connect/{token}")
def connect_page(token: str):
    _connect_session_or_404(token)
    return FileResponse(os.path.join(HERE, "dashboard", "connect.html"))


@app.post("/v1/connect/{token}/setup-intent")
def connect_setup_intent(token: str):
    """SetupIntent for the Connect page's Payment Element (token-authed,
    no admin login needed)."""
    session = _connect_session_or_404(token)
    try:
        si = tlt_billing.create_setup_intent(session["stripe_customer_id"])
    except tlt_billing.BillingError as exc:
        raise HTTPException(502, str(exc))
    return {
        "client_secret": si["client_secret"],
        "publishable_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        "name": session["name"],
        "monthly_cap_cents": session["monthly_cap_cents"],
    }


@app.post("/v1/connect/{token}/finalize")
def connect_finalize(token: str, body: ConnectFinalizeBody):
    """Complete Connect: verify the payment method was saved, then issue the
    rail API key (on_command tier, per-send approval OFF, bound to the
    monthly cap). The raw key is returned exactly once."""
    session = _connect_session_or_404(token)
    try:
        si = tlt_billing.get_setup_intent(body.setup_intent_id)
    except tlt_billing.BillingError as exc:
        raise HTTPException(502, str(exc))
    if si["status"] != "succeeded":
        raise HTTPException(
            402, "payment method not saved yet — complete the payment step "
                 "on the connect page first")
    raw = "lms_" + secrets.token_urlsafe(32)
    key_id = db.create_key(
        session["name"], _hash(raw), raw[:12],
        tier="on_command",
        monthly_cap_cents=session["monthly_cap_cents"],
        require_per_send_approval=False,
        requested_monthly_cap_cents=session["monthly_cap_cents"],
    )
    db.set_key_stripe_customer(key_id, session["stripe_customer_id"])
    db.set_key_owner_email(key_id, session["owner_email"])
    # Risk engine from birth: L0 caps; the Connect-requested monthly budget
    # is kept as a ceiling the customer can only lower, never raise —
    # enforced monthly = min(requested, risk level monthly).
    caps = risk.initialize_key(
        key_id,
        requested_monthly_cap_cents=session["monthly_cap_cents"],
        actor="connect")["caps"]
    db.complete_connect_session(session["id"], key_id)
    db.audit("connect", "connect.completed", "api_key", key_id,
             {"session_id": session["id"], "tier": "on_command",
              "monthly_cap_cents": caps["monthly_cents"],
              "requested_monthly_cap_cents": session["monthly_cap_cents"],
              "risk_level": 0,
              "require_per_send_approval": False})
    return {
        "id": key_id,
        "name": session["name"],
        "api_key": raw,
        "tier": "on_command",
        "require_per_send_approval": False,
        "monthly_cap_cents": caps["monthly_cents"],
        "risk_level": 0,
        "warning": "Store this key now. It is never shown again.",
    }


# ---- agent: drafts & sends ---------------------------------------------------
@app.post("/v1/drafts")
def create_draft(body: DraftBody, key=Depends(require_key)):
    try:
        return core.create_draft(
            key,
            to=body.to.model_dump(exclude_none=True),
            from_address=body.from_address.model_dump(exclude_none=True),
            html=body.html,
            color=body.color,
            quantity=body.quantity,
            description=body.description,
            certified=body.certified,
        )
    except core.AppError as exc:
        raise _err(exc)


@app.post("/v1/drafts/pdf")
async def create_draft_pdf(
    file: UploadFile = File(...),
    to: str = Form(...),
    from_address: str = Form(..., alias="from"),
    color: bool = Form(default=False),
    quantity: int = Form(default=1),
    description: Optional[str] = Form(default=None),
    certified: bool = Form(default=False),
    key=Depends(require_key),
):
    """Upload a customer PDF as letter content (multipart/form-data).

    Form fields: file (the PDF), to + from (JSON address objects),
    color, quantity, description, certified (USPS Certified Mail with
    Electronic Return Receipt). 10MB cap, magic-byte validated.
    """
    try:
        to_addr = json.loads(to)
        from_addr = json.loads(from_address)
    except (ValueError, TypeError):
        raise HTTPException(400, "to/from must be JSON address objects")
    if not isinstance(to_addr, dict) or not isinstance(from_addr, dict):
        raise HTTPException(400, "to/from must be JSON address objects")
    if quantity < 1 or quantity > 1000:
        raise HTTPException(400, "quantity must be between 1 and 1000")
    pdf_bytes = await file.read()
    try:
        return core.create_draft_pdf(
            key,
            to=to_addr,
            from_address=from_addr,
            pdf_bytes=pdf_bytes,
            filename=file.filename or "letter.pdf",
            color=color,
            quantity=quantity,
            description=description,
            certified=certified,
        )
    except core.AppError as exc:
        raise _err(exc)


@app.post("/v1/sends")
def request_send(body: SendBody, key=Depends(require_key)):
    try:
        return core.request_send(key, body.draft_id)
    except core.AppError as exc:
        raise _err(exc)


@app.get("/v1/sends/{request_id}")
def send_status(request_id: int, key=Depends(require_key)):
    try:
        return core.get_status(request_id)
    except core.AppError as exc:
        raise _err(exc)


# ---- customer self-service ----------------------------------------------------
# Key-scoped endpoints for the customer dashboard (dashboard/customer.html,
# served at GET /customer). Auth is the same X-API-Key as the agent
# endpoints; every response is filtered strictly to the calling key's own
# data — a key can never see another key's sends or account details.

def _customer_send_row(r) -> dict:
    """Serialize one send_requests row for the calling customer."""
    to = json.loads(r["to_json"])
    quote = json.loads(r["quote_json"])
    return {
        "request_id": r["id"],
        "status": r["status"],
        "created_at": r["created_at"],
        "decided_at": r["decided_at"],
        "approved_by": r["approved_by"],
        "quantity": r["quantity"],
        "certified": bool(r["certified"]),
        "recipient_name": to.get("name", ""),
        "recipient_address": {
            "line1": to.get("address_line1", ""),
            "line2": to.get("address_line2", "") or "",
            "city": to.get("address_city", ""),
            "state": to.get("address_state", ""),
            "zip": to.get("address_zip", ""),
            "country": to.get("address_country", "US"),
        },
        "provider_letter_id": r["provider_letter_id"],
        "failure_reason": r["failure_reason"],
        "amount_cents": r["amount_cents"],
        "cost_breakdown": {
            "base_total": quote.get("base_total"),
            "service_fee_total": quote.get("service_fee_total"),
            "estimated_total": quote.get("estimated_total"),
        },
    }


@app.get("/v1/sends")
def my_sends(status: Optional[str] = None, key=Depends(require_key)):
    """List the calling key's send requests, newest first. Optional
    ?status= filter (PENDING, SENT, REJECTED, FAILED)."""
    rows = db.list_send_requests_for_key(key["id"], status)
    return {"sends": [_customer_send_row(r) for r in rows]}


@app.get("/v1/account")
def my_account(key=Depends(require_key)):
    """The calling key's account summary for the customer dashboard."""
    frozen, frozen_reason = risk.check_frozen(key)
    caps = risk.effective_caps(key)
    return {
        "name": key["name"],
        "key_prefix": key["key_prefix"],
        "tier": key["tier"],
        "require_per_send_approval": risk.effective_require_approval(key),
        "risk_level": key["risk_level"],
        "frozen": frozen,
        "frozen_reason": frozen_reason,
        "monthly_cap_cents": caps.get("monthly_cents"),
        "spent_month_cents": db.spend_month_cents(key["id"]),
        "owner_email": key["owner_email"],
        "has_payment_method": bool(key["stripe_customer_id"]),
        "created_at": key["created_at"],
    }


@app.get("/customer")
def customer_page():
    return FileResponse(os.path.join(HERE, "dashboard", "customer.html"))


# ---- admin: approvals ---------------------------------------------------------
@app.get("/v1/admin/sends")
def list_sends(status: Optional[str] = None, _=Depends(require_admin)):
    import json

    rows = db.list_send_requests(status)
    out = []
    for r in rows:
        d = dict(r)
        d["quote"] = json.loads(d.pop("quote_json"))
        out.append(d)
    return {"sends": out}


@app.post("/v1/admin/sends/{request_id}/approve")
def approve(request_id: int, _=Depends(require_admin)):
    try:
        return core.approve_send(request_id, approver="admin")
    except core.AppError as exc:
        raise _err(exc)


@app.post("/v1/admin/sends/{request_id}/reject")
def reject(body: RejectBody, request_id: int, _=Depends(require_admin)):
    try:
        return core.reject_send(request_id, approver="admin", reason=body.reason)
    except core.AppError as exc:
        raise _err(exc)


@app.get("/v1/admin/history")
def history(limit: int = 200, _=Depends(require_admin)):
    return {"events": [dict(r) for r in db.recent_audit(limit)]}


# ---- admin: trust tiers & billing ------------------------------------------------
def _get_key_or_404(key_id: int):
    row = db.get_key(key_id)
    if not row:
        raise HTTPException(404, "API key not found")
    return row


@app.post("/v1/admin/keys/{key_id}/tier")
def set_tier(key_id: int, body: TierBody, _=Depends(require_admin)):
    _get_key_or_404(key_id)
    db.set_key_tier(key_id, body.tier)
    db.audit("admin", "key.tier_set", "api_key", key_id, {"tier": body.tier})
    return {"id": key_id, "tier": body.tier}


def _caps_body_to_override(body: CapsBody) -> dict:
    """Translate CapsBody (monthly_cap_cents, ...) to the override engine's
    key names (monthly_cents, ...), dropping fields that weren't provided."""
    mapping = {
        "monthly_cap_cents": "monthly_cents",
        "daily_cap_cents": "daily_cents",
        "per_send_cap_cents": "per_send_cents",
    }
    return {new: v for old, new in mapping.items()
            if (v := getattr(body, old)) is not None}


@app.post("/v1/admin/keys/{key_id}/caps")
def set_caps(key_id: int, body: CapsBody, _=Depends(require_admin)):
    """Set caps directly. Routed through the risk override mechanism so the
    engine stays the single owner of caps — this endpoint is now equivalent
    to POST /v1/admin/keys/{id}/risk/override {"caps": {...}}. Only the
    provided fields are overridden; the rest stay risk-derived."""
    _get_key_or_404(key_id)
    provided = _caps_body_to_override(body)
    if not provided:
        raise HTTPException(
            400, "pass daily_cap_cents, per_send_cap_cents and/or "
                 "monthly_cap_cents")
    view = risk.apply_override(key_id, {"caps": provided}, actor="admin")
    return {"id": key_id,
            "caps_cents": view["effective_caps_cents"],
            "override": view["override"]}


class RiskOverrideBody(BaseModel):
    level: Optional[int] = Field(default=None, ge=0, le=3)
    caps: Optional[CapsBody] = None
    frozen: Optional[bool] = None
    reason: Optional[str] = Field(default=None, max_length=500)
    clear: Optional[bool] = None


class RiskEventBody(BaseModel):
    kind: str = Field(pattern="^(flag|failed_payment|chargeback|velocity_spike)$")
    detail: Optional[dict] = None


@app.get("/v1/admin/keys/{key_id}/risk")
def get_key_risk(key_id: int, _=Depends(require_admin)):
    """Full risk picture: score, level, caps, reasons, freeze state, stats."""
    try:
        return risk.get_risk_view(key_id)
    except KeyError:
        raise HTTPException(404, "API key not found")


@app.post("/v1/admin/keys/{key_id}/risk/override")
def override_key_risk(key_id: int, body: RiskOverrideBody,
                      _=Depends(require_admin)):
    """Admin override: {"level": 0-3} pins the enforced level's caps,
    {"caps": {...}} enforces exact caps, {"frozen": false} unfreezes a key,
    {"clear": true} returns to fully risk-derived caps. Everything is
    audited with reasons=["admin override"]."""
    _get_key_or_404(key_id)
    payload = body.model_dump(exclude_none=True)
    if payload.get("caps") is not None:
        payload["caps"] = _caps_body_to_override(body.caps)
    try:
        return risk.apply_override(key_id, payload, actor="admin")
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/v1/admin/keys/{key_id}/risk/event")
def report_risk_event(key_id: int, body: RiskEventBody,
                      _=Depends(require_admin)):
    """Report an off-band risk event (e.g. a chargeback spotted in the Stripe
    dashboard, a manual abuse report). Applies the same downgrade
    side-effects as detected events: flag -> demote 1 level + 7d forced
    approval; failed_payment/chargeback -> freeze; velocity_spike -> halve
    daily cap 24h. Audited."""
    _get_key_or_404(key_id)
    result = risk.record_event(key_id, body.kind, body.detail or {},
                               actor="admin")
    return {"id": key_id, "event": body.kind, **result}


@app.post("/v1/admin/keys/{key_id}/per-send-approval")
def set_per_send_approval(key_id: int, body: PerSendApprovalBody,
                          _=Depends(require_admin)):
    """Opt a key into (require=true) or out of (require=false) the per-send
    Allow/Deny prompt. Rail-connected keys default to false (zero prompts);
    admin-issued keys default to true."""
    _get_key_or_404(key_id)
    db.set_key_require_per_send_approval(key_id, body.require)
    db.audit("admin", "key.per_send_approval_set", "api_key", key_id,
             {"require_per_send_approval": body.require})
    return {"id": key_id, "require_per_send_approval": body.require}


@app.get("/v1/admin/keys/{key_id}/spend")
def key_spend(key_id: int, _=Depends(require_admin)):
    row = _get_key_or_404(key_id)
    frozen, frozen_reason = risk.check_frozen(row)
    return {
        "id": key_id,
        "name": row["name"],
        "tier": row["tier"],
        "require_per_send_approval": risk.effective_require_approval(row),
        "risk_level": row["risk_level"],
        "risk_score": row["risk_score"],
        "frozen": frozen,
        "frozen_reason": frozen_reason,
        "daily_cap_cents": row["daily_cap_cents"],
        "per_send_cap_cents": row["per_send_cap_cents"],
        "monthly_cap_cents": row["monthly_cap_cents"],
        "effective_caps_cents": risk.effective_caps(row),
        "spent_today_cents": db.spend_today_cents(key_id),
        "spent_month_cents": db.spend_month_cents(key_id),
        "sends_today": db.sends_today_count(key_id),
        "avg_daily_sends_7d": round(db.avg_daily_sends_7d(key_id), 2),
        "has_payment_method": bool(row["stripe_customer_id"]),
        "owner_email": row["owner_email"],
    }


@app.post("/v1/admin/keys/{key_id}/email")
def set_owner_email(key_id: int, body: EmailBody, _=Depends(require_admin)):
    _get_key_or_404(key_id)
    email = _check_email(body.owner_email)
    db.set_key_owner_email(key_id, email)
    db.audit("admin", "key.email_set", "api_key", key_id, {"owner_email": email})
    return {"id": key_id, "owner_email": email}


@app.post("/v1/admin/keys/{key_id}/stripe-customer")
def create_stripe_customer(key_id: int, body: StripeCustomerBody,
                           _=Depends(require_admin)):
    """Create a Stripe Customer (test mode) and attach it to the key.

    The customer then saves a card / Apple Pay / Google Pay via the
    SetupIntent from /setup-intent + Stripe's Payment Element.
    """
    row = _get_key_or_404(key_id)
    try:
        customer = tlt_billing.create_customer(
            name=body.name or row["name"],
            email=body.email or row["owner_email"],
            metadata={"api_key_id": str(key_id)},
        )
    except tlt_billing.BillingError as exc:
        raise HTTPException(502, str(exc))
    db.set_key_stripe_customer(key_id, customer["id"])
    db.audit("admin", "key.stripe_customer_set", "api_key", key_id,
             {"stripe_customer_id": customer["id"]})
    return {"id": key_id, "stripe_customer_id": customer["id"]}


@app.post("/v1/admin/keys/{key_id}/setup-intent")
def create_setup_intent(key_id: int, _=Depends(require_admin)):
    """Return a SetupIntent client_secret for the Payment Element so the
    customer can save a card (Apple Pay / Google Pay appear automatically
    once the domain is HTTPS + verified)."""
    row = _get_key_or_404(key_id)
    if not row["stripe_customer_id"]:
        raise HTTPException(400, "no Stripe customer on this key yet — "
                                 "POST /stripe-customer first")
    try:
        si = tlt_billing.create_setup_intent(row["stripe_customer_id"])
    except tlt_billing.BillingError as exc:
        raise HTTPException(502, str(exc))
    return {"id": key_id, "client_secret": si["client_secret"]}


@app.get("/v1/billing/config")
def billing_config():
    return {
        "publishable_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        "test_mode": True,
    }


@app.get("/billing")
def billing_page():
    return FileResponse(os.path.join(HERE, "dashboard", "billing.html"))


@app.post("/v1/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Stripe webhook skeleton: verifies the signature, audits the event.

    charge.dispute.created is wired into the risk engine: the disputed
    payment intent is matched to the API key via billing receipts, a
    chargeback risk event is recorded, and the key is frozen pending admin
    review."""
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = tlt_billing.verify_webhook(payload, sig)
    except tlt_billing.BillingError as exc:
        raise HTTPException(400, str(exc))
    db.audit("stripe", f"webhook.{event['type']}", "stripe_event", None,
             {"event_id": event.get("id")})
    if event["type"] == "charge.dispute.created":
        obj = (event.get("data") or {}).get("object") or {}
        pi_id = obj.get("payment_intent")
        if pi_id:
            key_id = db.get_key_id_by_stripe_payment_id(pi_id)
            if key_id:
                risk.record_event(
                    key_id, "chargeback",
                    {"stripe_dispute_id": obj.get("id"),
                     "payment_intent": pi_id,
                     "amount_cents": obj.get("amount")},
                    actor="stripe")
    # payment_intent.succeeded / payment_intent.payment_failed are audited
    # here; charges are driven by approve_send, so no state change is needed.
    return {"received": True, "type": event["type"]}


# ---- one-tap Allow/Deny approval links -------------------------------------------
def _approval_page_html(request_id: int, token: str) -> str:
    row = db.get_send_request(request_id)
    if not row:
        raise HTTPException(404, "send request not found")
    to = json.loads(row["to_json"])
    quote = json.loads(row["quote_json"])
    kind = "PDF letter" if row["pdf_path"] else "letter"
    item_lines = [
        f"{kind} — {row['quantity']} copie(s), "
        f"{'color' if row['color'] else 'black & white'}"
        + (f", {row['page_count']} page(s) per copy" if row["page_count"] else "")
        + (", certified mail + return receipt" if row["certified"] else "")
    ]
    # Customer-facing figures: the single gross total they will be charged.
    # row["amount_cents"] is the gross stored at request_send time.
    net_cents = int(round(quote["estimated_total"] * 100))
    gross_cents = row["amount_cents"] or tlt_billing.gross_up(net_cents)
    processing_cents = gross_cents - net_cents
    return tlt_approvals.render_page(
        service_name=core.SERVICE_NAME,
        kind="letter",
        recipient_lines=[
            to.get("name", ""),
            to.get("address_line1", ""),
            f"{to.get('address_city', '')}, {to.get('address_state', '')} "
            f"{to.get('address_zip', '')}".strip(),
        ],
        item_lines=item_lines,
        cost_lines=[
            ("Provider base (est.)", f"${quote['base_total']:.2f}"),
            ("3L-Group service fee", f"${quote['service_fee_total']:.2f}"),
            ("Processing", f"${processing_cents / 100:.2f}"),
        ],
        total_str=f"${gross_cents / 100:.2f}",
        expires_human="in 24 hours",
        allow_path=f"/a/{token}/allow",
        deny_path=f"/a/{token}/deny",
    )


@app.get("/a/{token}", response_class=HTMLResponse)
def approval_page(token: str):
    try:
        request_id = core._preview_token(token)
    except core.AppError as exc:
        return HTMLResponse(
            tlt_approvals.render_result(
                ok=False, title="Link expired",
                message="This approval link is invalid, expired, or was already "
                        "used. Check the dashboard for the current status."),
            status_code=exc.code,
        )
    return HTMLResponse(_approval_page_html(request_id, token))


@app.post("/a/{token}/allow", response_class=HTMLResponse)
def approval_allow(token: str):
    try:
        request_id = core.consume_approval_token(token)
    except core.AppError as exc:
        return HTMLResponse(
            tlt_approvals.render_result(
                ok=False, title="Link expired",
                message="This approval link is invalid, expired, or was already "
                        "used."),
            status_code=exc.code,
        )
    try:
        result = core.approve_send(request_id, approver="approval_link")
    except core.AppError as exc:
        return HTMLResponse(
            tlt_approvals.render_result(
                ok=False, title="Could not send",
                message=str(exc)),
            status_code=exc.code,
        )
    charge_note = (f"Charged {result['stripe_payment_id']}."
                   if result.get("stripe_payment_id")
                   else "No payment method on file — not charged.")
    return HTMLResponse(
        tlt_approvals.render_result(
            ok=True, title="Approved ✓",
            message=f"Letter #{request_id} is on its way. {charge_note} "
                    f"A receipt was emailed to the key owner."))


@app.post("/a/{token}/deny", response_class=HTMLResponse)
def approval_deny(token: str):
    try:
        request_id = core.consume_approval_token(token)
    except core.AppError as exc:
        return HTMLResponse(
            tlt_approvals.render_result(
                ok=False, title="Link expired",
                message="This approval link is invalid, expired, or was already "
                        "used."),
            status_code=exc.code,
        )
    try:
        core.reject_send(request_id, approver="approval_link",
                         reason="denied via approval link")
    except core.AppError as exc:
        return HTMLResponse(
            tlt_approvals.render_result(
                ok=False, title="Could not deny", message=str(exc)),
            status_code=exc.code,
        )
    return HTMLResponse(
        tlt_approvals.render_result(
            ok=True, title="Denied",
            message=f"Send request #{request_id} was cancelled. "
                    f"Nothing was mailed and nothing was charged."))
