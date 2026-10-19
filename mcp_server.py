"""MCP server for the letter-mailing service (streamable HTTP).

Exposes these tools to AI agents:
  - draft_letter:  validate address + screen content + price quote. NEVER sends.
                   Accepts HTML (html=...) or a server-local PDF (pdf_path=...).
  - request_send:  create a send request. approval-tier keys get a PENDING
                   request + one-tap Allow/Deny link; on_command keys may
                   auto-send inside guardrails (caps, screening, anomaly).
  - send_status:   check status incl. approval state, tier decision, billing.
  - create_fax_draft: validate number + count pages + screen + quote a fax
                   DRAFT from a server-local PDF. NEVER transmits.
  - approve_fax:   record the approval for a fax draft (the send gate).
  - send_fax:      transmit an APPROVED fax via Phaxio.
  - get_fax_status: fax lifecycle state + live Phaxio delivery status.

A letter is mailed only after approval: a human tap (dashboard or Allow link)
or the on_command trust tier. There is no tool that sends directly. A fax
likewise transmits only after approve_fax — there is no zero-prompt fax path.

Auth: the server validates the agent's API key on every call. The key is
issued by the service owner via POST /v1/keys on the REST API.

Run:
    LMS_API_KEY=... LOB_API_KEY=test_... .venv/bin/python mcp_server.py
Then point any MCP-compatible client at http://127.0.0.1:8001/mcp
"""
from __future__ import annotations

import hashlib
import os

from fastmcp import FastMCP

import core
import db
import mail_provider
import phaxio_client

mcp = FastMCP("3l-group-technology-letter-mail")
db.init_db()
mail_provider.guard_live_key()
phaxio_client.guard_live_key()


def _auth(api_key: str):
    if not api_key:
        raise ValueError("api_key is required (issue one via POST /v1/keys)")
    row = db.get_key_by_hash(hashlib.sha256(api_key.encode()).hexdigest())
    if not row:
        raise ValueError("invalid or revoked api_key")
    return row


@mcp.tool()
def draft_letter(
    api_key: str,
    to: dict,
    from_address: dict,
    html: str | None = None,
    pdf_path: str | None = None,
    color: bool = False,
    quantity: int = 1,
    description: str | None = None,
    certified: bool = False,
) -> dict:
    """Create a letter DRAFT: verifies the recipient address with PostGrid,
    screens content, and returns a price quote. This NEVER mails anything.

    Pass EITHER html (letter body as an HTML string) OR pdf_path (path to a
    PDF file on the server). PDF text is extracted for screening; if
    extraction fails the draft is kept but flagged needs_review.

    Args:
        api_key: your 3L-Group Technology API key (lms_...).
        to: recipient address dict with name, address_line1, address_city,
            address_state, address_zip (US).
        from_address: sender address dict, same shape as `to`.
        html: letter body as an HTML string (use this OR pdf_path).
        pdf_path: server-local path to a PDF to mail instead of html.
        color: True for color printing, False for black & white.
        quantity: number of identical letters (10+ gets the bulk rate).
        description: optional label for your own records.
        certified: OPT-IN ONLY — True for USPS Certified Mail with Electronic
            Receipt (tracked, signature on delivery). Defaults to False, which
            sends standard First Class mail. Only set True when the customer
            explicitly asked for certified mail: it costs significantly more
            (PostGrid's certified rate + the flat $1.00 service fee, roughly
            $11 per letter). Never default this to True.
    """
    key_row = _auth(api_key)
    try:
        if pdf_path:
            if not os.path.isfile(pdf_path):
                return {"error": f"pdf_path not found: {pdf_path}", "code": 422}
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            return core.create_draft_pdf(
                key_row, to, from_address, pdf_bytes,
                filename=os.path.basename(pdf_path),
                color=color, quantity=quantity, description=description,
                certified=certified,
            )
        return core.create_draft(
            key_row, to, from_address, html or "", color, quantity,
            description, certified,
        )
    except core.AppError as exc:
        return {"error": str(exc), "code": exc.code}


@mcp.tool()
def request_send(api_key: str, draft_id: int) -> dict:
    """Request that a draft be mailed.

    approval-tier keys: creates a PENDING request and returns a one-tap
    Allow/Deny approval_url for the human. Nothing is mailed until Allow.
    on_command keys: may auto-send inside guardrails (spend caps, content
    screening, anomaly detection); anything outside them stays PENDING.

    Args:
        api_key: your 3L-Group Technology API key (lms_...).
        draft_id: the draft_id returned by draft_letter.
    """
    key_row = _auth(api_key)
    try:
        return core.request_send(key_row, draft_id)
    except core.AppError as exc:
        return {"error": str(exc), "code": exc.code}


@mcp.tool()
def send_status(api_key: str, request_id: int) -> dict:
    """Check a send request: status (PENDING/SENT/REJECTED/FAILED), the key's
    trust tier and tier decision, approval state (+approval_url while pending),
    and billing state (Stripe charge id when billed).

    Args:
        api_key: your 3L-Group Technology API key (lms_...).
        request_id: the request_id returned by request_send.
    """
    _auth(api_key)
    try:
        return core.get_status(request_id)
    except core.AppError as exc:
        return {"error": str(exc), "code": exc.code}


@mcp.tool()
def create_fax_draft(
    api_key: str,
    to_number: str,
    pdf_path: str,
    description: str | None = None,
) -> dict:
    """Create a fax DRAFT: validates the recipient number, counts the PDF's
    pages, screens its text, and returns a price quote (Phaxio per-page
    cost x pages + the flat $1.00 service fee). This NEVER transmits
    anything — call approve_fax, then send_fax.

    Args:
        api_key: your 3L-Group Technology API key (lms_...).
        to_number: recipient fax number — 10-digit US number or a full
            E.164 number starting with +.
        pdf_path: path to the PDF file on the server to fax.
        description: optional label for your own records.
    """
    key_row = _auth(api_key)
    try:
        if not os.path.isfile(pdf_path):
            return {"error": f"pdf_path not found: {pdf_path}", "code": 422}
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        return core.create_fax_draft(
            key_row, to_number, pdf_bytes,
            filename=os.path.basename(pdf_path), description=description,
        )
    except core.AppError as exc:
        return {"error": str(exc), "code": exc.code}


@mcp.tool()
def approve_fax(api_key: str, fax_id: int) -> dict:
    """Approve a fax DRAFT so it can be transmitted. The content is
    re-screened at approval time. Only the key that created the fax can
    approve it. Nothing is transmitted by this call — send_fax does that.

    Args:
        api_key: your 3L-Group Technology API key (lms_...).
        fax_id: the fax_id returned by create_fax_draft.
    """
    key_row = _auth(api_key)
    try:
        return core.approve_fax(key_row, fax_id)
    except core.AppError as exc:
        return {"error": str(exc), "code": exc.code}


@mcp.tool()
def send_fax(api_key: str, fax_id: int) -> dict:
    """Transmit an APPROVED fax via Phaxio. Refuses drafts that were never
    approved and faxes that were already sent — a fax can be transmitted
    exactly once.

    Args:
        api_key: your 3L-Group Technology API key (lms_...).
        fax_id: the fax_id returned by create_fax_draft.
    """
    key_row = _auth(api_key)
    try:
        return core.send_fax(key_row, fax_id)
    except core.AppError as exc:
        return {"error": str(exc), "code": exc.code}


@mcp.tool()
def get_fax_status(api_key: str, fax_id: int) -> dict:
    """Check a fax: lifecycle status (DRAFT/APPROVED/SENT/FAILED), page
    count, quote, and — once sent — the live delivery status from Phaxio.

    Args:
        api_key: your 3L-Group Technology API key (lms_...).
        fax_id: the fax_id returned by create_fax_draft.
    """
    key_row = _auth(api_key)
    try:
        return core.get_fax_status(key_row, fax_id)
    except core.AppError as exc:
        return {"error": str(exc), "code": exc.code}


if __name__ == "__main__":
    host = os.environ.get("LMS_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("LMS_MCP_PORT", "8001"))
    mcp.run(transport="streamable-http", host=host, port=port)
