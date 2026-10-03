"""Thin wrapper around the Lob print-and-mail API.

Secrets: the Lob API key comes ONLY from the LOB_API_KEY environment
variable. Nothing is hardcoded here or anywhere else in this project.

Test mode: a Lob *test* key (starts with ``test_``) makes Lob simulate every
call — no real mail is ever sent. A live key with a funded postage balance is
required before anything physical goes out.
"""
from __future__ import annotations

import os
import uuid

import httpx

LOB_API_BASE = "https://api.lob.com/v1"


class LobError(RuntimeError):
    """Raised when Lob is unreachable, misconfigured, or rejects a call."""


def _api_key() -> str:
    key = os.environ.get("LOB_API_KEY", "").strip()
    if not key:
        raise LobError(
            "LOB_API_KEY is not set. Create a Lob account at https://lob.com, "
            "copy your API key, and export LOB_API_KEY."
        )
    return key


def is_test_mode() -> bool:
    """True when using a Lob test key (simulated sends, nothing mailed)."""
    return _api_key().startswith("test_")


def _client() -> httpx.Client:
    # Lob uses HTTP Basic Auth: the API key as the username, blank password.
    # trust_env=False: never route Lob traffic (and the API key) through
    # ambient HTTP(S)_PROXY env vars; talk to api.lob.com directly.
    return httpx.Client(
        base_url=LOB_API_BASE, auth=(_api_key(), ""), timeout=30.0, trust_env=False
    )


def _raise_for_lob(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        err = (data.get("error") or {}).get("message") or resp.text
        raise LobError(f"Lob API error {resp.status_code}: {err}")
    return data


def _address_fields(prefix: str, address: dict) -> dict:
    """Flatten {'name':..,'address_line1':..} into Lob's to[name] form encoding."""
    out: dict = {}
    for field in (
        "name",
        "address_line1",
        "address_line2",
        "address_city",
        "address_state",
        "address_zip",
        "address_country",
    ):
        value = address.get(field)
        if value:
            out[f"{prefix}[{field}]"] = value
    return out


def verify_address(address: dict) -> dict:
    """Verify a US address via Lob. Returns deliverability summary.

    Raises LobError if Lob is unreachable/misconfigured.
    """
    payload = {
        field: address[field]
        for field in (
            "name",
            "address_line1",
            "address_line2",
            "address_city",
            "address_state",
            "address_zip",
            "address_country",
        )
        if address.get(field)
    }
    with _client() as client:
        resp = client.post("/us_verifications", data=payload)
    data = _raise_for_lob(resp)
    return {
        "deliverability": data.get("deliverability"),
        "components": data.get("components", {}),
        "raw": data,
    }


def create_letter(
    to: dict,
    from_address: dict,
    html: str | None = None,
    pdf_path: str | None = None,
    color: bool = False,
    description: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create (mail) a letter via Lob. Returns Lob's letter object.

    Content is either an ``html`` string or a ``pdf_path`` to a local PDF —
    Lob accepts PDF uploads natively (multipart ``file``).

    Pass a stable ``idempotency_key`` (e.g. the 3LT send-request ID) so a
    retried submission returns the already-created letter instead of a
    second one; when omitted a random key is generated.

    With a test key this is simulated. With a live key and funded balance,
    this puts a real letter in the mail — callers must gate this behind a
    human approval record.
    """
    if not html and not pdf_path:
        raise LobError("create_letter needs html or pdf_path")
    payload: dict = {"color": "true" if color else "false"}
    if description:
        payload["description"] = description
    payload.update(_address_fields("to", to))
    payload.update(_address_fields("from", from_address))
    headers = {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}
    with _client() as client:
        if pdf_path:
            with open(pdf_path, "rb") as f:
                resp = client.post(
                    "/letters",
                    data=payload,
                    files={"file": ("letter.pdf", f, "application/pdf")},
                    headers=headers,
                )
        else:
            resp = client.post(
                "/letters", data={**payload, "file": html}, headers=headers
            )
    return _raise_for_lob(resp)


def get_letter(letter_id: str) -> dict:
    """Retrieve a Lob letter by id."""
    with _client() as client:
        resp = client.get(f"/letters/{letter_id}")
    return _raise_for_lob(resp)
