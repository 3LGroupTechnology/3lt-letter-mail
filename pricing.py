"""Flat pricing for the letter-mailing service.

$1.00 service fee per letter, on top of PostGrid's base cost. One price,
no tiers, no volume games — a dollar for convenience.

The base cost is PostGrid's per-letter charge. PostGrid bills actuals, so the
base is configurable via POSTGRID_BASE_COST_USD and every quote is labeled an
estimate. Default is PostGrid's published US B&W first-class single-page
letter price (verified 2026-10-01).

Certified letters (USPS Certified Mail with Electronic Return Receipt) use
PostGrid's published certified per-letter price instead of the first-class
base — configurable via POSTGRID_CERTIFIED_COST_USD. The $1.00 service fee is
unchanged for certified.
"""
from __future__ import annotations

import os

FLAT_FEE_USD = 1.00


def base_cost_estimate() -> float:
    """Estimated PostGrid per-letter base cost in USD (env-overridable)."""
    try:
        return float(os.environ.get("POSTGRID_BASE_COST_USD", "0.97"))
    except ValueError:
        return 0.97


def certified_cost_estimate() -> float:
    """Estimated PostGrid per-letter cost for USPS Certified Mail with
    Electronic Return Receipt in USD (env-overridable).

    Default is PostGrid's published 1-page B&W certified + electronic return
    receipt price (postgrid.co.uk pricing page). Labeled an estimate —
    PostGrid bills actuals.
    """
    try:
        return float(os.environ.get("POSTGRID_CERTIFIED_COST_USD", "9.51"))
    except ValueError:
        return 9.51


def quote(base_cost: float, quantity: int, certified: bool = False) -> dict:
    """Return a price breakdown for `quantity` letters.

    `base_cost` is the per-letter first-class estimate; when `certified` is
    true the certified per-letter estimate is used instead. The $1.00 flat
    service fee applies unchanged either way.
    """
    if quantity < 1:
        raise ValueError("quantity must be >= 1")
    fee = FLAT_FEE_USD
    per_letter = certified_cost_estimate() if certified else base_cost
    service_total = round(fee * quantity, 2)
    base_total = round(per_letter * quantity, 2)
    return {
        "tier": "flat",
        "quantity": quantity,
        "certified": bool(certified),
        "base_cost_per_letter": round(per_letter, 2),
        "service_fee_per_letter": fee,
        "base_total": base_total,
        "service_fee_total": service_total,
        "estimated_total": round(base_total + service_total, 2),
        "note": "Base cost is an estimate; PostGrid bills actuals.",
    }
