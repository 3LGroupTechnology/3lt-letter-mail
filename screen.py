"""Abuse screening hook.

Runs before a draft is created AND again before a human-approved send is
executed. This MVP uses a simple blocklist; swap in a real classifier by
replacing _classifier_score() (see TODO).
"""
from __future__ import annotations

import re

# Simple blocklist: obvious threats, fraud lures, and impersonation of
# government/law-enforcement senders. Case-insensitive substring match on
# text stripped of HTML tags.
BLOCKED_PHRASES = (
    "i will kill you",
    "i'm going to kill you",
    "bomb threat",
    "anthrax",
    "wire the money",
    "send bitcoin",
    "send usdt",
    "gift card payment",
    "your account will be closed",
    "final notice of arrest",
    "warrant for your arrest",
    "you have won a lottery",
    "claim your prize",
    "social security number suspended",
)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def _classifier_score(text: str) -> float:
    """Return 0.0 (clean) .. 1.0 (abusive).

    TODO: replace with a real classifier (e.g. a moderation API or a local
    model). For the MVP this always returns 0.0 and the blocklist does the
    work; keep the hook so the upgrade is a one-function change.
    """
    return 0.0


CLASSIFIER_THRESHOLD = 0.8


def screen_content(html: str) -> tuple[bool, str]:
    """Return (allowed, reason). allowed=False blocks the send pipeline."""
    text = _strip_html(html).lower()
    for phrase in BLOCKED_PHRASES:
        if phrase in text:
            return False, f"blocked phrase matched: {phrase!r}"
    score = _classifier_score(text)
    if score >= CLASSIFIER_THRESHOLD:
        return False, f"classifier flagged content (score {score:.2f})"
    return True, "passed"
