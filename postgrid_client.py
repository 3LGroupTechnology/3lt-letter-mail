"""Thin wrapper around the PostGrid print-and-mail API.

Secrets: the PostGrid API key comes ONLY from the POSTGRID_API_KEY environment
variable. Address verification is a separate PostGrid product with its own key
(POSTGRID_AV_API_KEY); when that is unset the print-and-mail key is reused.
Nothing is hardcoded here or anywhere else in this project.

Test mode: a PostGrid *test* key (starts with ``test_``, e.g. ``test_sk_...``)
makes PostGrid simulate every call — no real mail is ever sent and test orders
never leave the sandbox. A live key is required before anything physical goes
out; live keys are refused unless the deployment explicitly allows them (see
mail_provider.py).

API notes (verified against PostGrid docs 2026-10-01):
- Print & Mail base: https://api.postgrid.com/print-mail/v1, auth via the
  ``x-api-key`` header. Letters: POST /letters (JSON for HTML content,
  multipart ``pdf`` file field for PDF content). Idempotent creates via the
  ``Idempotency-Key`` header. Certified mail: ``mailingClass`` =
  ``"certified_return_receipt"`` (USPS Certified + Electronic Return Receipt,
  tracked with signature on delivery).
- Address verification: POST https://api.postgrid.com/v1/addver/verifications
  with a structured ``address`` object; the response ``data.status`` is one of
  ``verified`` / ``corrected`` / ``failed``.
"""
from __future__ import annotations

import os
import uuid

import httpx

POSTGRID_MAIL_BASE = "https://api.postgrid.com/print-mail/v1"
POSTGRID_AV_BASE = "https://api.postgrid.com/v1"


class PostGridError(RuntimeError):
    """Raised when PostGrid is unreachable, misconfigured, or rejects a call."""


def _api_key() -> str:
    key = os.environ.get("POSTGRID_API_KEY", "").strip()
    if not key:
        raise PostGridError(
            "POSTGRID_API_KEY is not set. Create a PostGrid account at "
            "https://www.postgrid.com, copy your Print & Mail API key from "
            "the dashboard Settings page, and export POSTGRID_API_KEY."
        )
    return key


def _av_api_key() -> str:
    # Address Verification is a separate PostGrid product with its own key;
    # fall back to the Print & Mail key when no dedicated key is configured.
    return os.environ.get("POSTGRID_AV_API_KEY", "").strip() or _api_key()


def is_test_mode() -> bool:
    """True when using a PostGrid test key (simulated sends, nothing mailed)."""
    return _api_key().startswith("test_")


def _client(base_url: str, api_key: str) -> httpx.Client:
    # PostGrid authenticates with the x-api-key header.
    # Proxy handling: the sandbox has no direct egress — outbound traffic
    # must go through the platform's egress proxy (HTTPS_PROXY). We pass it
    # explicitly with trust_env=False rather than letting httpx read the
    # environment, because this sandbox's no_proxy value contains bracketed
    # IPv6 literals (e.g. [::1]) that httpx's URL parser rejects
    # ("Invalid port: ':1]'"). On hosts without proxy vars set (e.g. the
    # Hetzner deploy) proxy=None and the client talks to api.postgrid.com
    # directly, exactly as before.
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    # The egress proxy MITMs TLS with its own CA; the platform publishes the
    # bundle at SSL_CERT_FILE (e.g. /run/hatch/egress-tls/ca-bundle.pem).
    # trust_env=False means httpx won't read that env var itself, so pass it
    # explicitly. Absent the var (e.g. Hetzner) this is True: normal system
    # certificate verification.
    verify = os.environ.get("SSL_CERT_FILE") or True
    return httpx.Client(
        base_url=base_url,
        headers={"x-api-key": api_key},
        timeout=30.0,
        trust_env=False,
        proxy=proxy_url,
        verify=verify,
    )


def _raise_for_postgrid(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        err = data.get("error") or {}
        message = err.get("message") or resp.text
        err_type = err.get("type")
        detail = f"{err_type}: {message}" if err_type else message
        raise PostGridError(f"PostGrid API error {resp.status_code}: {detail}")
    return data


_COUNTRY_ALIASES = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.a.": "US",
    "canada": "CA",
}


def _country_code(value: str | None) -> str:
    if not value:
        return "US"
    v = value.strip()
    if len(v) == 2:
        return v.upper()
    return _COUNTRY_ALIASES.get(v.lower(), "US")


def _split_name(name: str) -> tuple[str, str]:
    """Split 'First Last' into (firstName, lastName) for PostGrid contacts."""
    parts = (name or "").strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def _to_postgrid_address(address: dict) -> dict:
    """Map our snake_case address dict onto PostGrid's contact fields."""
    first, last = _split_name(address.get("name", ""))
    out: dict = {
        "firstName": first,
        "addressLine1": address.get("address_line1", ""),
        "city": address.get("address_city", ""),
        "provinceOrState": address.get("address_state", ""),
        "postalOrZip": address.get("address_zip", ""),
        "countryCode": _country_code(address.get("address_country")),
    }
    if last:
        out["lastName"] = last
    if address.get("address_line2"):
        out["addressLine2"] = address["address_line2"]
    return out


def _to_postgrid_av_address(address: dict) -> dict:
    """Map our address dict onto the Address Verification structured format."""
    out: dict = {
        "line1": address.get("address_line1", ""),
        "city": address.get("address_city", ""),
        "provinceOrState": address.get("address_state", ""),
        "postalOrZip": address.get("address_zip", ""),
        "country": _country_code(address.get("address_country")),
    }
    if address.get("address_line2"):
        out["line2"] = address["address_line2"]
    return {k: v for k, v in out.items() if v}


def verify_address(address: dict) -> dict:
    """Verify a US/CA address via PostGrid. Returns deliverability summary.

    PostGrid's verification ``status`` maps onto our deliverability buckets:
    ``verified``/``corrected`` -> deliverable, ``failed`` -> undeliverable.

    Raises PostGridError if PostGrid is unreachable/misconfigured.
    """
    payload = {"address": _to_postgrid_av_address(address)}
    with _client(POSTGRID_AV_BASE, _av_api_key()) as client:
        resp = client.post("/addver/verifications", json=payload)
    body = _raise_for_postgrid(resp)
    data = body.get("data") or {}
    status = str(data.get("status", "failed")).lower()
    errors = data.get("errors") or {}
    if status == "failed" or errors:
        deliverability = "undeliverable"
    else:
        # "verified" and "corrected" both mean the postal service can
        # deliver; "corrected" just means PostGrid standardized the input.
        deliverability = "deliverable"
    return {
        "deliverability": deliverability,
        "components": data.get("details", {}),
        "raw": body,
    }


def _letter_payload(
    to: dict,
    from_address: dict,
    html: str | None,
    color: bool,
    description: str | None,
    certified: bool = False,
    address_placement: str = "top_first_page",
) -> dict:
    payload: dict = {
        "to": _to_postgrid_address(to),
        "from": _to_postgrid_address(from_address),
        "color": bool(color),
        "doubleSided": False,
        # PostGrid stamps the recipient block onto the first page; keep the
        # top of page one clear in rendered HTML. For PDF uploads of finished
        # documents (court filings etc.) use "insert_blank_page" so the
        # address goes on its own sheet and the document is untouched.
        "addressPlacement": address_placement,
    }
    if certified:
        # USPS Certified Mail with Electronic Return Receipt: PostGrid tracks
        # the piece end-to-end and returns the delivery signature.
        payload["mailingClass"] = "certified_return_receipt"
    if description:
        payload["description"] = description[:255]
    if html:
        payload["html"] = html
    return payload


def _flatten_for_multipart(payload: dict) -> dict:
    """Flatten nested to/from dicts into PostGrid's to[field] form encoding."""
    flat: dict = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            for sub, subval in value.items():
                flat[f"{key}[{sub}]"] = subval
        elif isinstance(value, bool):
            flat[key] = "true" if value else "false"
        else:
            flat[key] = value
    return flat


def _normalize_letter(body: dict) -> dict:
    letter_id = body.get("id")
    if not letter_id:
        raise PostGridError("PostGrid did not return a letter id.")
    return {
        "id": letter_id,
        "expected_delivery_date": body.get("expectedDeliveryDate")
        or body.get("sendDate"),
        "status": body.get("status"),
        "raw": body,
    }


def create_letter(
    to: dict,
    from_address: dict,
    html: str | None = None,
    pdf_path: str | None = None,
    color: bool = False,
    description: str | None = None,
    idempotency_key: str | None = None,
    certified: bool = False,
    address_placement: str = "top_first_page",
) -> dict:
    """Create (mail) a letter via PostGrid. Returns a normalized letter dict.

    Content is either an ``html`` string or a ``pdf_path`` to a local PDF —
    PostGrid accepts PDF uploads natively (multipart ``pdf`` field``).

    ``certified=True`` sends USPS Certified Mail with Electronic Return
    Receipt (PostGrid ``mailingClass="certified_return_receipt"``): tracked
    end-to-end with the recipient's signature captured on delivery.

    ``address_placement`` is PostGrid's addressPlacement: "top_first_page"
    (default) or "insert_blank_page" (address on its own sheet — use for
    finished PDF documents like court filings so nothing is overprinted).

    Every create carries an ``Idempotency-Key`` so a retried submission
    returns the already-created letter instead of a second one. Pass a stable
    ``idempotency_key`` (e.g. the 3LT send-request ID); when omitted a random
    key is generated.

    With a test key this is simulated. With a live key this puts a real
    letter in the mail — callers must gate this behind a human approval
    record.
    """
    if not html and not pdf_path:
        raise PostGridError("create_letter needs html or pdf_path")
    payload = _letter_payload(to, from_address, html, color, description,
                              certified, address_placement)
    headers = {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}
    with _client(POSTGRID_MAIL_BASE, _api_key()) as client:
        if pdf_path:
            with open(pdf_path, "rb") as f:
                resp = client.post(
                    "/letters",
                    data=_flatten_for_multipart(payload),
                    files={"pdf": ("letter.pdf", f, "application/pdf")},
                    headers=headers,
                )
        else:
            resp = client.post("/letters", json=payload, headers=headers)
    return _normalize_letter(_raise_for_postgrid(resp))


def get_letter(letter_id: str) -> dict:
    """Retrieve a PostGrid letter by id."""
    with _client(POSTGRID_MAIL_BASE, _api_key()) as client:
        resp = client.get(f"/letters/{letter_id}")
    return _normalize_letter(_raise_for_postgrid(resp))
