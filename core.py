"""Shared business logic for the letter-mailing service.

Used by both api.py (REST) and mcp_server.py (MCP tools) so the approval
gate, screening, pricing, billing, and trust tiers behave identically
everywhere.

Golden rule: a letter is NEVER sent without a standing approval record —
either the one-time Connect authorization (rail-connected on_command keys:
payment method + monthly cap on file, sends flow with zero prompts inside
server-side guardrails) or a per-send human decision (approval tier, or
on_command keys with per-send approval opted in — dashboard or one-tap
Allow link). approve_send() enforces this.

Billing: when the key has a Stripe customer on file, approval authorizes
the charge off-session, PostGrid fulfills, then the charge is captured. If Lob
fails, the authorization is voided — customers never pay for unsent mail.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import sys
from datetime import datetime, timezone

# Shared 3L-Group Technology modules (billing, tiers, approval links, mailer).
_SHARED = os.environ.get("TLT_SHARED_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "3lt-shared"
)
if os.path.isdir(_SHARED) and _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

import db
import mail_provider
import pricing
import risk
import screen
import tlt_approvals
import tlt_billing
import tlt_mailer
import tlt_receipts
import tlt_tiers

SERVICE_NAME = "3L-Group Technology — AI Letter Mail"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

_APPROVAL_SECRET = os.environ.get("APPROVAL_TOKEN_SECRET")
if not _APPROVAL_SECRET:
    _APPROVAL_SECRET = secrets.token_urlsafe(32)
    print(
        "[lms] APPROVAL_TOKEN_SECRET not set; generated an ephemeral secret — "
        "approval links will not survive a restart. Set it in production.",
        flush=True,
    )

MAX_PDF_BYTES = int(os.environ.get("LMS_MAX_PDF_BYTES", str(10 * 1024 * 1024)))
UPLOADS_DIR = os.environ.get(
    "LMS_UPLOADS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
)


class AppError(Exception):
    """User-facing error with an HTTP-style code."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor(key_row) -> str:
    return f"key:{key_row['key_prefix']}"


def _amount_cents(quote: dict) -> int:
    return int(round(quote["estimated_total"] * 100))


def _enrich_quote(quote: dict) -> dict:
    """Add customer-facing gross fields to a pricing.quote() dict.

    ``estimated_total`` stays the NET (PostGrid base + 3LT service fee). The new
    fields are what the customer actually sees and is charged:
      - ``processing_fee_total``: the Stripe pass-through, in dollars.
      - ``total_charged``: the single customer-facing total, in dollars.
    """
    parts = tlt_billing.price_breakdown(_amount_cents(quote))
    quote["processing_fee_total"] = round(parts["processing_cents"] / 100, 2)
    quote["total_charged"] = round(parts["gross_cents"] / 100, 2)
    return quote


# ---- PDF helpers ------------------------------------------------------------
def _pdf_info(pdf_bytes: bytes):
    """Return (page_count, extracted_text). extracted_text is None when text
    extraction fails — the draft is kept but flagged needs_review."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return max(1, round(len(pdf_bytes) / 4500)), None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
    except Exception:
        return max(1, round(len(pdf_bytes) / 4500)), None
    try:
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return page_count, None
    return page_count, text


def _store_pdf(draft_id: int, pdf_bytes: bytes) -> str:
    draft_dir = os.path.join(UPLOADS_DIR, f"draft_{draft_id}")
    os.makedirs(draft_dir, exist_ok=True)
    path = os.path.join(draft_dir, "source.pdf")
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    return path


# ---- drafts -------------------------------------------------------------------
def create_draft(
    api_key_row,
    to: dict,
    from_address: dict,
    html: str,
    color: bool = False,
    quantity: int = 1,
    description: str | None = None,
    certified: bool = False,
) -> dict:
    """Validate, screen, quote, and store an HTML draft. Does NOT send anything."""
    if quantity < 1 or quantity > 1000:
        raise AppError("quantity must be between 1 and 1000")
    if not html or not html.strip():
        raise AppError("html letter body is required")

    ok, reason = screen.screen_content(html)
    if not ok:
        db.audit(_actor(api_key_row), "draft.blocked", "draft", None, {"reason": reason})
        risk.record_event(api_key_row["id"], "flag",
                          {"reason": reason, "stage": "draft", "kind": "html"},
                          actor=_actor(api_key_row))
        raise AppError(f"Content screening failed: {reason}", 422)

    verification = _verify(to)
    quote = pricing.quote(pricing.base_cost_estimate(), quantity,
                          certified=certified)
    _enrich_quote(quote)  # estimated_total stays NET; total_charged is gross
    draft_id = db.insert_draft(
        api_key_row["id"], to, from_address, html, color, quantity,
        description, quote, verification, certified=certified,
    )
    db.audit(
        _actor(api_key_row), "draft.create", "draft", draft_id,
        {"quantity": quantity, "estimated_total": quote["estimated_total"],
         "kind": "html", "certified": certified},
    )
    return {
        "draft_id": draft_id,
        "kind": "html",
        "certified": certified,
        "mailing_class": "certified_return_receipt" if certified
        else "first_class",
        "quote": quote,
        "address_verification": {"deliverability": verification.get("deliverability")},
        "test_mode": mail_provider.is_test_mode(),
    }


def create_draft_pdf(
    api_key_row,
    to: dict,
    from_address: dict,
    pdf_bytes: bytes,
    filename: str = "letter.pdf",
    color: bool = False,
    quantity: int = 1,
    description: str | None = None,
    certified: bool = False,
) -> dict:
    """Validate, screen (via text extraction), quote, and store a PDF draft.

    If text extraction fails the draft is KEPT but flagged needs_review —
    it can never auto-send on the on_command tier.
    """
    if quantity < 1 or quantity > 1000:
        raise AppError("quantity must be between 1 and 1000")
    if not pdf_bytes:
        raise AppError("PDF file is required", 422)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise AppError(
            f"PDF is {len(pdf_bytes)} bytes; limit is {MAX_PDF_BYTES} bytes", 413
        )
    if not pdf_bytes.startswith(b"%PDF-"):
        raise AppError("uploaded file is not a PDF (bad magic bytes)", 422)

    page_count, text = _pdf_info(pdf_bytes)
    needs_review = text is None
    if not needs_review:
        ok, reason = screen.screen_content(text or "")
        if not ok:
            db.audit(_actor(api_key_row), "draft.blocked", "draft", None,
                     {"reason": reason, "kind": "pdf"})
            risk.record_event(api_key_row["id"], "flag",
                              {"reason": reason, "stage": "draft", "kind": "pdf"},
                              actor=_actor(api_key_row))
            raise AppError(f"Content screening failed: {reason}", 422)

    verification = _verify(to)
    quote = pricing.quote(pricing.base_cost_estimate(), quantity,
                          certified=certified)
    _enrich_quote(quote)  # estimated_total stays NET; total_charged is gross
    draft_id = db.insert_draft(
        api_key_row["id"], to, from_address, "", color, quantity,
        description, quote, verification, needs_review=needs_review,
        certified=certified,
    )
    pdf_path = _store_pdf(draft_id, pdf_bytes)
    db.set_draft_pdf(draft_id, pdf_path, page_count, needs_review)
    db.audit(
        _actor(api_key_row), "draft.create", "draft", draft_id,
        {"quantity": quantity, "estimated_total": quote["estimated_total"],
         "kind": "pdf", "page_count": page_count, "needs_review": needs_review,
         "filename": filename, "certified": certified},
    )
    return {
        "draft_id": draft_id,
        "kind": "pdf",
        "page_count": page_count,
        "needs_review": needs_review,
        "certified": certified,
        "mailing_class": "certified_return_receipt" if certified
        else "first_class",
        "quote": quote,
        "address_verification": {"deliverability": verification.get("deliverability")},
        "test_mode": mail_provider.is_test_mode(),
    }


def _verify(to: dict) -> dict:
    try:
        verification = mail_provider.verify_address(to)
    except mail_provider.ProviderError as exc:
        raise AppError(str(exc), 503)
    if verification.get("deliverability") == "undeliverable":
        raise AppError(
            "Recipient address looks undeliverable per PostGrid verification.", 422
        )
    return verification


# ---- approval links -------------------------------------------------------------
def issue_approval_link(request_id: int) -> tuple[str, str]:
    """Mint a single-use approval token. Returns (url, expires_human)."""
    token, exp = tlt_approvals.make_token(_APPROVAL_SECRET, request_id)
    db.create_approval_token(
        tlt_approvals.token_hash(token), request_id,
        datetime.fromtimestamp(exp, timezone.utc).isoformat(),
    )
    expires_human = datetime.fromtimestamp(exp, timezone.utc).strftime(
        "%b %d, %Y %H:%M UTC"
    )
    return f"{PUBLIC_BASE_URL}/a/{token}", expires_human


def _preview_token(token: str) -> int:
    request_id = tlt_approvals.parse_token(_APPROVAL_SECRET, token)
    if request_id is None:
        raise AppError("approval link is invalid or expired", 410)
    active = db.get_active_token_for_request(request_id, _now())
    if not active or active["token_hash"] != tlt_approvals.token_hash(token):
        raise AppError("approval link was already used or expired", 410)
    return request_id


def consume_approval_token(token: str) -> int:
    """Validate + atomically consume a token. Returns request_id."""
    request_id = tlt_approvals.parse_token(_APPROVAL_SECRET, token)
    if request_id is None:
        raise AppError("approval link is invalid or expired", 410)
    row = db.consume_approval_token(tlt_approvals.token_hash(token), _now())
    if not row:
        raise AppError("approval link was already used or expired", 410)
    return request_id


def ensure_approval_link(request_id: int) -> str | None:
    """Return a usable approval URL for a PENDING request, minting one if needed."""
    active = db.get_active_token_for_request(request_id, _now())
    if active:
        # We store only the hash, so mint a fresh token for surfacing.
        pass
    url, _ = issue_approval_link(request_id)
    return url


# ---- send requests ---------------------------------------------------------------
def request_send(api_key_row, draft_id: int) -> dict:
    """Create a send request, then apply the key's trust tier.

    Rail model: keys connected through the one-time Connect flow
    (on_command tier, require_per_send_approval=False) flow with ZERO
    prompts — the Connect authorization is the standing approval.

    approval tier, or on_command with require_per_send_approval=True
    (opt-in): stays PENDING, human taps Allow/Deny on the one-tap link.

    Hard stops (402, before any send_request row exists — nothing mailed,
    nothing charged):
      - monthly spend cap would be exceeded -> "raise your cap"
      - zero-prompt auto path with no payment method on file
    """
    draft = db.get_draft(draft_id)
    if not draft:
        raise AppError("draft not found", 404)
    quote = json.loads(draft["quote_json"])
    # NET stays net (PostGrid base + 3LT fee); the customer is charged the
    # grossed-up figure so Stripe's processing rides on top invisibly.
    net_cents = _amount_cents(quote)
    charge_cents = tlt_billing.gross_up(net_cents)
    actor = _actor(api_key_row)
    key_id = api_key_row["id"]

    # Risk engine: frozen keys are hard-stopped before anything else.
    frozen, frozen_reason = risk.check_frozen(api_key_row)
    if frozen:
        db.audit(actor, "send.blocked", "send_request", None,
                 {"reason": "key_frozen", "frozen_reason": frozen_reason})
        raise AppError(f"This API key is frozen: {frozen_reason}", 403)

    # Caps are risk-derived (AI-set), not static defaults.
    caps = risk.effective_caps(api_key_row)
    monthly_cap_cents = caps["monthly_cents"]
    spent_month_cents = db.spend_month_cents(key_id)
    # Caps are enforced on the CHARGED (gross) amount — the cap is the
    # customer-facing promise "never charged more than $X".
    if spent_month_cents + charge_cents > monthly_cap_cents:
        db.audit(actor, "send.blocked", "send_request", None,
                 {"reason": "over_monthly_cap",
                  "spent_month_cents": spent_month_cents,
                  "monthly_cap_cents": monthly_cap_cents,
                  "send_total_cents": charge_cents})
        raise AppError(
            f"Monthly spend cap reached (${monthly_cap_cents / 100:.2f}). "
            "Raise your cap to send more — nothing was mailed or charged.",
            402,
        )

    tier = draft_tier(api_key_row)
    # Per-send approval is forced for 7 days after any content flag, on top
    # of the key's own require_per_send_approval setting.
    require_approval = risk.effective_require_approval(api_key_row)
    zero_prompt = tier == "on_command" and not require_approval

    if zero_prompt and not api_key_row["stripe_customer_id"]:
        db.audit(actor, "send.blocked", "send_request", None,
                 {"reason": "no_payment_method_on_file"})
        raise AppError(
            "No payment method on file for automatic sending. Complete the "
            "one-time Connect flow (POST /v1/connect) or attach a payment "
            "method — nothing was mailed or charged.",
            402,
        )

    request_id = db.insert_send_request(draft_id, charge_cents)

    if not zero_prompt:
        # Per-send human approval (opt-in for on_command, default for the
        # approval tier): the one-tap Allow/Deny link is the flow.
        reason = "approval_tier" if tier != "on_command" \
            else "per_send_approval_required"
        decision = {"mode": "pending", "reason": reason}
        db.update_send_request(request_id, tier_decision=f"pending:{reason}")
        db.audit(actor, "send.tier_decision", "send_request", request_id,
                 {"tier": tier, **decision})
        db.audit(actor, "send.request", "send_request", request_id,
                 {"draft_id": draft_id})
        approval_url = ensure_approval_link(request_id)
        return {
            "request_id": request_id,
            "status": "PENDING",
            "tier": tier,
            "tier_decision": reason,
            "approval_required": True,
            "approval_url": approval_url,
            "quote": quote,
            "message": "Send request created. Tap the approval link (Allow/Deny) "
                       "before anything is mailed.",
        }

    # Zero-prompt rail path: screen + guardrails decide.
    if draft["pdf_path"]:
        try:
            with open(draft["pdf_path"], "rb") as f:
                _, pdf_text = _pdf_info(f.read())
        except OSError:
            pdf_text = None
        screen_content = pdf_text or ""
        needs_review = pdf_text is None or bool(draft["needs_review"])
    else:
        screen_content, needs_review = draft["html"] or "", False
    screened_ok, screened_reason = screen.screen_content(screen_content)

    decision = tlt_tiers.decide(
        tier=tier,
        send_total_cents=charge_cents,
        spent_today_cents=db.spend_today_cents(key_id),
        sends_today=db.sends_today_count(key_id),
        avg_daily_sends_7d=db.avg_daily_sends_7d(key_id),
        screened_ok=screened_ok,
        screened_reason=screened_reason,
        needs_review=needs_review,
        daily_cap_cents=caps["daily_cents"],
        per_send_cap_cents=caps["per_send_cents"],
        spent_month_cents=spent_month_cents,
        monthly_cap_cents=caps["monthly_cents"],
    )
    # Risk events feed the scoring model: a flag demotes the key one level
    # and forces per-send approval for 7 days; a velocity spike halves the
    # daily cap for 24h. Both are audited with reasons.
    if not screened_ok:
        risk.record_event(key_id, "flag",
                          {"reason": decision["reason"], "stage": "send",
                           "request_id": request_id},
                          actor=actor)
    elif decision["reason"] == "anomaly":
        risk.record_event(key_id, "velocity_spike",
                          {"request_id": request_id,
                           "sends_today": db.sends_today_count(key_id),
                           "avg_daily_sends_7d": round(db.avg_daily_sends_7d(key_id), 2)},
                          actor=actor)
    db.update_send_request(
        request_id, tier_decision=f"{decision['mode']}:{decision['reason']}"
    )
    db.audit(actor, "send.tier_decision", "send_request", request_id,
             {"tier": tier, **decision})
    db.audit(actor, "send.request", "send_request", request_id, {"draft_id": draft_id})

    if decision["mode"] == "auto":
        result = approve_send(request_id, approver="tier:on_command")
        result["tier"] = tier
        result["tier_decision"] = decision["reason"]
        result["approval_required"] = False
        result["quote"] = quote
        return result

    approval_url = ensure_approval_link(request_id)
    return {
        "request_id": request_id,
        "status": "PENDING",
        "tier": tier,
        "tier_decision": decision["reason"],
        "approval_required": True,
        "approval_url": approval_url,
        "quote": quote,
        "message": "Send request created. Tap the approval link (Allow/Deny) "
                   "before anything is mailed.",
    }


def draft_tier(api_key_row) -> str:
    tier = api_key_row["tier"] if "tier" in api_key_row.keys() else "approval"
    return tier if tier in tlt_tiers.VALID_TIERS else "approval"


# ---- approval gate + billing ----------------------------------------------------------
def approve_send(request_id: int, approver: str) -> dict:
    """Approve a PENDING request: re-screen, authorize Stripe, send to PostGrid,
    capture the charge, email the receipt. Each step is audited."""
    row = db.get_send_request(request_id)
    if not row:
        raise AppError("send request not found", 404)
    if row["status"] != "PENDING":
        raise AppError(f"request is {row['status']}, only PENDING can be approved", 409)

    # Re-screen at approval time — content rules may have changed since draft.
    ok, reason = screen.screen_content(row["html"] or "")
    if not ok:
        db.update_send_request(
            request_id, status="FAILED", failure_reason=f"screening: {reason}",
            decided_at=_now(),
        )
        db.audit(approver, "send.blocked", "send_request", request_id, {"reason": reason})
        risk.record_event(row["api_key_id"], "flag",
                          {"reason": reason, "stage": "approval",
                           "request_id": request_id},
                          actor=approver)
        raise AppError(f"Content screening failed at approval: {reason}", 422)

    key_row = db.get_key(row["api_key_id"])
    frozen, frozen_reason = risk.check_frozen(key_row)
    if frozen:
        db.audit(approver, "send.blocked", "send_request", request_id,
                 {"reason": "key_frozen", "frozen_reason": frozen_reason})
        raise AppError(f"This API key is frozen: {frozen_reason}", 403)
    quote = json.loads(row["quote_json"])
    # amount_cents on the row is the GROSS customer-facing charge (grossed up
    # at request_send time). Fall back to grossing the net quote for rows
    # written before the pass-through change.
    amount_cents = row["amount_cents"] or tlt_billing.gross_up(_amount_cents(quote))
    to = json.loads(row["to_json"])
    from_address = json.loads(row["from_json"])

    def _fulfill():
        return mail_provider.create_letter(
            to=to,
            from_address=from_address,
            html=row["html"] or None,
            pdf_path=row["pdf_path"],
            color=bool(row["color"]),
            description=row["description"],
            # Stable per send-request: a retried fulfillment reuses the same
            # provider-side letter instead of mailing twice.
            idempotency_key=f"3lt-send-{request_id}",
            # Certified flag was chosen at draft time and priced into the
            # quote; it flows straight through to PostGrid here.
            certified=bool(row["certified"]),
        )

    try:
        letter, receipt = tlt_billing.settle(
            db, key_row, request_id, amount_cents, "usd", _fulfill, approver
        )
    except tlt_billing.BillingError as exc:
        db.update_send_request(
            request_id, status="FAILED",
            failure_reason=f"billing: {exc}", decided_at=_now(),
        )
        db.audit(approver, "send.failed", "send_request", request_id,
                 {"error": f"billing: {exc}"})
        # Failed payment freezes the key's sends until an admin unfreezes.
        risk.record_event(key_row["id"], "failed_payment",
                          {"reason": str(exc), "request_id": request_id},
                          actor=approver)
        raise AppError(f"Billing failed: {exc}", 502)
    except mail_provider.ProviderError as exc:
        # settle() already voided the Stripe authorization.
        db.update_send_request(
            request_id, status="FAILED", failure_reason=str(exc), decided_at=_now()
        )
        db.audit(approver, "send.failed", "send_request", request_id,
                 {"error": str(exc)})
        raise AppError(f"PostGrid send failed: {exc}", 502)

    decided_at = _now()
    db.update_send_request(
        request_id,
        status="SENT",
        provider_letter_id=letter.get("id") if letter else None,
        approved_by=approver,
        decided_at=decided_at,
    )
    db.audit(
        approver, "send.approved", "send_request", request_id,
        {"provider_letter_id": (letter.get("id") if letter else None),
         "tier": draft_tier(key_row)},
    )
    # Clean send: feed the risk model so caps rise with good history.
    risk.recompute(key_row["id"], actor=approver)
    _maybe_email_receipt(key_row, row, request_id, quote, receipt, approver,
                         decided_at)
    return {
        "request_id": request_id,
        "status": "SENT",
        "provider_letter_id": letter.get("id") if letter else None,
        "expected_delivery_date": letter.get("expected_delivery_date") if letter else None,
        "tier": draft_tier(key_row),
        "billed": receipt is not None,
        "stripe_payment_id": receipt["stripe_payment_id"] if receipt else None,
        "test_mode": mail_provider.is_test_mode(),
    }


def reject_send(request_id: int, approver: str, reason: str | None = None) -> dict:
    row = db.get_send_request(request_id)
    if not row:
        raise AppError("send request not found", 404)
    if row["status"] != "PENDING":
        raise AppError(f"request is {row['status']}, only PENDING can be rejected", 409)
    db.update_send_request(
        request_id, status="REJECTED", failure_reason=reason, decided_at=_now()
    )
    db.audit(approver, "send.rejected", "send_request", request_id, {"reason": reason})
    # No charge ever happens on the reject path — billing only runs on approve.
    return {"request_id": request_id, "status": "REJECTED"}


# ---- itemized email receipts ----------------------------------------------------------
def _maybe_email_receipt(key_row, send_row, request_id: int, quote: dict,
                         receipt, approver: str, decided_at: str) -> None:
    """Email an itemized receipt to the key owner's address after every
    successful send. Never raises — mailer failures are audited, not fatal."""
    email = key_row["owner_email"] if "owner_email" in key_row.keys() else None
    if not email:
        db.audit(approver, "receipt.skipped", "send_request", request_id,
                 {"reason": "no_owner_email_on_key"})
        return
    to = json.loads(send_row["to_json"])
    kind = "PDF" if send_row["pdf_path"] else "HTML"
    item_lines = [
        f"Letter ({kind}) — {send_row['quantity']} copie(s), "
        f"{'color' if send_row['color'] else 'black & white'}"
        + (f", {send_row['page_count']} page(s) per copy" if send_row["page_count"] else "")
        + (", certified mail + return receipt" if send_row["certified"] else "")
    ]
    recipient_lines = [
        to.get("name", ""),
        to.get("address_line1", ""),
        f"{to.get('address_city', '')}, {to.get('address_state', '')} "
        f"{to.get('address_zip', '')}".strip(),
    ]
    # Four-line breakdown: base + 3LT fee (net) + processing = total charged.
    # The 3LT service-fee line is ALWAYS the full fee — processing rides on
    # top and never erodes it.
    net_cents = _amount_cents(quote)
    gross_cents = (receipt["amount_cents"] if receipt
                   else tlt_billing.gross_up(net_cents))
    processing_cents = gross_cents - net_cents
    try:
        subject, html_body, text_body = tlt_receipts.build_receipt(
            service_name=SERVICE_NAME,
            item_lines=item_lines,
            recipient_lines=recipient_lines,
            decided_at=decided_at,
            cost_lines=[
                ("Provider base (est.)", f"${quote['base_total']:.2f}"),
                ("3L-Group service fee", f"${quote['service_fee_total']:.2f}"),
                ("Processing", f"${processing_cents / 100:.2f}"),
            ],
            total_str=f"${gross_cents / 100:.2f}",
            charge_id=receipt["stripe_payment_id"] if receipt else None,
            tier=draft_tier(key_row),
            request_id=request_id,
            key_label=key_row["name"],
        )
        info = tlt_mailer.send_receipt(
            email, subject, html_body, text_body,
            {"request_id": request_id, "service": "letter-mail"},
        )
        db.audit(approver, "receipt.sent", "send_request", request_id,
                 {"to": email, "backend": info.get("backend")})
    except tlt_mailer.MailerError as exc:
        db.audit(approver, "receipt.failed", "send_request", request_id,
                 {"to": email, "error": str(exc)})


# ---- status -------------------------------------------------------------------------------
def get_status(request_id: int) -> dict:
    row = db.get_send_request(request_id)
    if not row:
        raise AppError("send request not found", 404)
    key_row = db.get_key(row["api_key_id"])
    tier = draft_tier(key_row) if key_row else "approval"

    approval_url = None
    if row["status"] == "PENDING":
        approval_url = ensure_approval_link(request_id)

    receipts = db.get_receipts_for_request(request_id)
    billing_state = None
    if receipts:
        r = receipts[0]
        billing_state = {
            "status": r["status"],
            "amount_cents": r["amount_cents"],
            "currency": r["currency"],
            "stripe_payment_id": r["stripe_payment_id"],
            "receipt_id": r["id"],
        }
    elif key_row and key_row["stripe_customer_id"]:
        billing_state = {"status": "not_charged_yet"}

    return {
        "request_id": row["id"],
        "status": row["status"],
        "tier": tier,
        "tier_decision": row["tier_decision"],
        "approval": {
            "required": row["status"] == "PENDING",
            "url": approval_url,
        },
        "billing": billing_state,
        "provider_letter_id": row["provider_letter_id"],
        "failure_reason": row["failure_reason"],
        "approved_by": row["approved_by"],
        "decided_at": row["decided_at"],
        "created_at": row["created_at"],
        "quote": json.loads(row["quote_json"]),
        "quantity": row["quantity"],
        "kind": "pdf" if row["pdf_path"] else "html",
        "test_mode": mail_provider.is_test_mode(),
    }
