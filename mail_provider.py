"""Selects the active print-and-mail provider for the letter service.

MAIL_PROVIDER env var: "postgrid" (default) or "lob". Both provider modules
expose the same interface — is_test_mode(), verify_address(), create_letter(),
get_letter() — and a provider-specific Error class, re-exported here as
ProviderError so callers stay provider-agnostic.

LIVE-KEY GUARD: a live provider key (starts with "live_") is refused unless
ALLOW_LIVE_MAIL=1 is set. Test keys simulate every send; nothing physical can
go out while this guard trips.
"""
from __future__ import annotations

import os

PROVIDER_NAME = os.environ.get("MAIL_PROVIDER", "postgrid").strip().lower()

if PROVIDER_NAME == "lob":
    from lob_client import (  # noqa: F401
        LobError as ProviderError,
        create_letter,
        get_letter,
        is_test_mode,
        verify_address,
    )
elif PROVIDER_NAME == "postgrid":
    from postgrid_client import (  # noqa: F401
        PostGridError as ProviderError,
        create_letter,
        get_letter,
        is_test_mode,
        verify_address,
    )
else:
    raise RuntimeError(
        f"Unknown MAIL_PROVIDER={PROVIDER_NAME!r}; expected 'postgrid' or 'lob'."
    )


def guard_live_key() -> None:
    """Refuse to boot with a live provider key unless explicitly allowed."""
    if PROVIDER_NAME == "lob":
        key = os.environ.get("LOB_API_KEY", "")
    else:
        key = os.environ.get("POSTGRID_API_KEY", "")
    if key.startswith("live_") and os.environ.get("ALLOW_LIVE_MAIL") != "1":
        raise RuntimeError(
            "Refusing a live mail-provider key: set ALLOW_LIVE_MAIL=1 to send "
            "real mail."
        )
