"""Thin wrapper around the Phaxio fax API (v2.1).

Secrets: the Phaxio API key and secret come ONLY from the PHAXIO_API_KEY /
PHAXIO_API_SECRET environment variables. Nothing is hardcoded here or
anywhere else in this project.

Test mode: mirroring the mail provider's convention, a Phaxio *test* key
(starts with ``test_``) makes Phaxio simulate every send — no real fax is
ever transmitted. A live key is required before anything real goes out;
live keys are refused unless the deployment explicitly allows them
(ALLOW_LIVE_FAX=1, see guard_live_key()).

API notes (Phaxio API v2.1):
- Base: https://api.phaxio.com/v2.1, auth is HTTP Basic with the API key
  as username and the API secret as password.
- Send: POST /faxes as multipart form — ``to`` (recipient number, E.164),
  ``file`` (uploaded document parts) or ``content_url`` (a URL Phaxio
  fetches). Response: {"success": true, "data": {"id": <int>, ...}}.
- Retrieve: GET /faxes/{id} — data carries ``status`` (e.g. queued /
  success / failure / partialSuccess), ``numPages`` and ``cost`` (cents).
- Phaxio v2.1 documents no idempotency support. send_fax() passes the
  caller's stable key as an ``Idempotency-Key`` header on a best-effort
  basis only — the real duplicate protection is the service-side status
  machine in core.py (an APPROVED fax can transition to SENT exactly once).
"""
from __future__ import annotations

import os
import uuid

import httpx

PHAXIO_BASE = "https://api.phaxio.com/v2.1"


class PhaxioError(RuntimeError):
    """Raised when Phaxio is unreachable, misconfigured, or rejects a call."""


def _credentials() -> tuple[str, str]:
    key = os.environ.get("PHAXIO_API_KEY", "").strip()
    secret = os.environ.get("PHAXIO_API_SECRET", "").strip()
    if not key or not secret:
        raise PhaxioError(
            "PHAXIO_API_KEY / PHAXIO_API_SECRET are not set. Create a "
            "Phaxio account at https://www.phaxio.com, copy your API key "
            "and secret from the console API settings page, and export "
            "PHAXIO_API_KEY and PHAXIO_API_SECRET."
        )
    return key, secret


def is_configured() -> bool:
    """True when both Phaxio credentials are present (never raises)."""
    return bool(os.environ.get("PHAXIO_API_KEY", "").strip()) and bool(
        os.environ.get("PHAXIO_API_SECRET", "").strip()
    )


def is_test_mode() -> bool:
    """True when using a Phaxio test key (simulated sends, nothing faxed)."""
    key, _ = _credentials()
    return key.startswith("test_")


def guard_live_key() -> None:
    """Refuse to boot with a live Phaxio key unless explicitly allowed.

    Mirrors mail_provider.guard_live_key(): test keys simulate every send;
    nothing physical can go out while this guard trips. Unconfigured fax
    (no credentials at all) is fine — fax endpoints report 503 until the
    credentials are set, the letter side is unaffected.
    """
    if not is_configured():
        return
    if not is_test_mode() and os.environ.get("ALLOW_LIVE_FAX") != "1":
        raise RuntimeError(
            "Refusing a live Phaxio key: set ALLOW_LIVE_FAX=1 to send "
            "real faxes."
        )


def _guard_send() -> None:
    """Send-time live check (defense in depth alongside guard_live_key(),
    in case the environment changed after boot)."""
    if not is_test_mode() and os.environ.get("ALLOW_LIVE_FAX") != "1":
        raise PhaxioError(
            "Refusing to send a live fax: set ALLOW_LIVE_FAX=1 to send "
            "real faxes."
        )


def _client() -> httpx.Client:
    # Phaxio authenticates with HTTP Basic (api key = username,
    # api secret = password).
    # Proxy handling matches postgrid_client._client: the sandbox has no
    # direct egress — outbound traffic must go through the platform's
    # egress proxy (HTTPS_PROXY), passed explicitly with trust_env=False;
    # the proxy's TLS bundle is at SSL_CERT_FILE. On hosts without proxy
    # vars set (e.g. the Hetzner deploy) proxy=None and the client talks
    # to api.phaxio.com directly.
    key, secret = _credentials()
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    verify = os.environ.get("SSL_CERT_FILE") or True
    return httpx.Client(
        base_url=PHAXIO_BASE,
        auth=(key, secret),
        timeout=30.0,
        trust_env=False,
        proxy=proxy_url,
        verify=verify,
    )


def _raise_for_phaxio(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or data.get("success") is False:
        message = (
            data.get("message")
            or (data.get("error") or {}).get("message")
            or resp.text
        )
        raise PhaxioError(f"Phaxio API error {resp.status_code}: {message}")
    return data


def _normalize_fax(body: dict) -> dict:
    data = body.get("data") or {}
    fax_id = data.get("id")
    if fax_id is None:
        raise PhaxioError("Phaxio did not return a fax id.")
    return {
        "id": str(fax_id),
        "status": data.get("status"),
        "num_pages": data.get("numPages"),
        "cost_cents": data.get("cost"),
        "is_test": data.get("isTest"),
        "raw": body,
    }


def send_fax(
    to: str,
    pdf_path: str | None = None,
    content_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Send a fax via Phaxio. Returns a normalized fax dict.

    Content is either a ``pdf_path`` to a local PDF (multipart ``file``
    upload) or a ``content_url`` Phaxio fetches itself — exactly one.

    ``idempotency_key`` is forwarded as an ``Idempotency-Key`` header on a
    best-effort basis (Phaxio v2.1 documents no idempotency support); the
    service-side status machine in core.py is the actual guard against
    double-sends. Pass a stable key anyway (e.g. the 3LT fax id) so the
    attempt is traceable.

    With a test key this is simulated. With a live key this transmits a
    real fax (and refuses outright unless ALLOW_LIVE_FAX=1) — callers
    must gate this behind a human approval record.
    """
    if not pdf_path and not content_url:
        raise PhaxioError("send_fax needs pdf_path or content_url")
    if pdf_path and content_url:
        raise PhaxioError("send_fax takes pdf_path OR content_url, not both")
    _guard_send()
    form: dict = {"to": to}
    if content_url:
        form["content_url"] = content_url
    headers = {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}
    with _client() as client:
        if pdf_path:
            with open(pdf_path, "rb") as f:
                resp = client.post(
                    "/faxes",
                    data=form,
                    files={"file": ("fax.pdf", f, "application/pdf")},
                    headers=headers,
                )
        else:
            resp = client.post("/faxes", data=form, headers=headers)
    return _normalize_fax(_raise_for_phaxio(resp))


def get_fax(fax_id: str) -> dict:
    """Retrieve a Phaxio fax by id (delivery status, pages, cost)."""
    with _client() as client:
        resp = client.get(f"/faxes/{fax_id}")
    return _normalize_fax(_raise_for_phaxio(resp))
