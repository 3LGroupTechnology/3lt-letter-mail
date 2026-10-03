"""SQLite persistence for drafts, send requests, API keys, and audit log.

Extended with: trust tiers + spend caps per API key, Stripe customer linkage,
owner email for receipts, billing receipts, single-use approval tokens, and
PDF draft support.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "LMS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "lms.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id INTEGER,
    to_json TEXT NOT NULL,
    from_json TEXT NOT NULL,
    html TEXT NOT NULL,
    color INTEGER NOT NULL DEFAULT 0,
    quantity INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    quote_json TEXT NOT NULL,
    verification_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS send_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    status TEXT NOT NULL DEFAULT 'PENDING',
    provider_letter_id TEXT,
    failure_reason TEXT,
    approved_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS billing_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    send_request_id INTEGER NOT NULL,
    api_key_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    stripe_payment_id TEXT,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS approval_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    request_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS connect_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    owner_email TEXT NOT NULL,
    monthly_cap_cents INTEGER NOT NULL,
    stripe_customer_id TEXT,
    api_key_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_risk_events_key_kind
    ON risk_events (api_key_id, kind);
"""

# Column migrations for databases created before the billing/tiers upgrade.
_MIGRATIONS = [
    ("api_keys", "tier", "TEXT NOT NULL DEFAULT 'approval'"),
    ("api_keys", "daily_cap_cents", "INTEGER NOT NULL DEFAULT 2500"),
    ("api_keys", "per_send_cap_cents", "INTEGER NOT NULL DEFAULT 5000"),
    ("api_keys", "stripe_customer_id", "TEXT"),
    ("api_keys", "owner_email", "TEXT"),
    # Invisible-rail upgrade: monthly budget (hard cap, 402 on exceed) and the
    # opt-in per-send approval flag. Existing keys are all admin-issued, so
    # require_per_send_approval defaults TRUE to preserve their behavior.
    ("api_keys", "monthly_cap_cents", "INTEGER NOT NULL DEFAULT 20000"),
    ("api_keys", "require_per_send_approval", "INTEGER NOT NULL DEFAULT 1"),
    ("drafts", "pdf_path", "TEXT"),
    ("drafts", "page_count", "INTEGER"),
    ("drafts", "needs_review", "INTEGER NOT NULL DEFAULT 0"),
    # Certified mail option: USPS Certified Mail with Electronic Return
    # Receipt requested on the draft. Existing drafts default to first class.
    ("drafts", "certified", "INTEGER NOT NULL DEFAULT 0"),
    ("send_requests", "amount_cents", "INTEGER"),
    ("send_requests", "tier_decision", "TEXT"),
    # Dynamic risk engine: score/level/caps owned by tlt_risk. risk_frozen
    # blocks sends (403); force_approval_until forces the per-send Allow/Deny
    # prompt; daily_cap_halved_until halves the daily cap (velocity events);
    # risk_override_json holds an admin override {"level": N} | {"caps": {...}};
    # requested_monthly_cap_cents is the customer's own (lower) budget from
    # Connect — enforced monthly = min(requested, risk level monthly).
    ("api_keys", "risk_level", "INTEGER NOT NULL DEFAULT 0"),
    ("api_keys", "risk_score", "INTEGER NOT NULL DEFAULT 50"),
    ("api_keys", "risk_reasons", "TEXT"),
    ("api_keys", "risk_updated_at", "TEXT"),
    ("api_keys", "risk_frozen", "INTEGER NOT NULL DEFAULT 0"),
    ("api_keys", "frozen_reason", "TEXT"),
    ("api_keys", "force_approval_until", "TEXT"),
    ("api_keys", "daily_cap_halved_until", "TEXT"),
    ("api_keys", "risk_override_json", "TEXT"),
    ("api_keys", "requested_monthly_cap_cents", "INTEGER"),
]


def _backfill_risk(conn) -> None:
    """Legacy rows (created before the risk engine): preserve their chosen
    monthly budget as the requested cap, then drop them to the L0 baseline
    ($50/mo enforced). Idempotent: only touches rows never risk-scored."""
    conn.execute(
        """UPDATE api_keys
           SET requested_monthly_cap_cents = monthly_cap_cents,
               monthly_cap_cents = 5000,
               risk_level = 0,
               risk_score = 50,
               risk_reasons = '["migrated: L0 baseline — caps now risk-derived"]',
               risk_updated_at = datetime('now')
           WHERE risk_updated_at IS NULL"""
    )


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    # Provider rewire (2026-10-01): Lob -> PostGrid. Rename the stored letter
    # id column on pre-existing databases; new databases get the new name
    # straight from SCHEMA.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(send_requests)")}
    if "lob_letter_id" in cols and "provider_letter_id" not in cols:
        conn.execute(
            "ALTER TABLE send_requests "
            "RENAME COLUMN lob_letter_id TO provider_letter_id"
        )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _backfill_risk(conn)


# ---- api keys -------------------------------------------------------------
# NOTE: monthly_cap_cents default (20000 = $200/mo) mirrors
# tlt_tiers.DEFAULT_MONTHLY_CAP_CENTS; kept literal here so db.py has no
# dependency on the shared modules.
# NOTE: monthly_cap_cents here is the *enforced* monthly cap, which the
# risk engine maintains (risk.recompute / risk.initialize_key). Pass
# requested_monthly_cap_cents for the customer's own lower budget from
# Connect; enforced monthly = min(requested, risk level monthly).
def create_key(name: str, key_hash: str, key_prefix: str,
               tier: str = "approval", monthly_cap_cents: int = 20000,
               require_per_send_approval: bool = True,
               requested_monthly_cap_cents: int | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO api_keys
               (name, key_hash, key_prefix, tier, monthly_cap_cents,
                require_per_send_approval, requested_monthly_cap_cents)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, key_hash, key_prefix, tier, monthly_cap_cents,
             1 if require_per_send_approval else 0,
             requested_monthly_cap_cents),
        )
        return cur.lastrowid


def get_key_by_hash(key_hash: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND revoked = 0", (key_hash,)
        ).fetchone()


def get_key(key_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()


def set_key_tier(key_id: int, tier: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE api_keys SET tier = ? WHERE id = ?", (tier, key_id))


def set_key_caps(key_id: int, daily_cap_cents=None, per_send_cap_cents=None,
                 monthly_cap_cents=None) -> None:
    sets, vals = [], []
    if daily_cap_cents is not None:
        sets.append("daily_cap_cents = ?")
        vals.append(daily_cap_cents)
    if per_send_cap_cents is not None:
        sets.append("per_send_cap_cents = ?")
        vals.append(per_send_cap_cents)
    if monthly_cap_cents is not None:
        sets.append("monthly_cap_cents = ?")
        vals.append(monthly_cap_cents)
    if not sets:
        return
    with connect() as conn:
        conn.execute(
            f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ?", vals + [key_id]
        )


def set_key_require_per_send_approval(key_id: int, require: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET require_per_send_approval = ? WHERE id = ?",
            (1 if require else 0, key_id),
        )


def set_key_stripe_customer(key_id: int, customer_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET stripe_customer_id = ? WHERE id = ?",
            (customer_id, key_id),
        )


def set_key_owner_email(key_id: int, email: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET owner_email = ? WHERE id = ?", (email, key_id)
        )


# ---- risk engine state ------------------------------------------------------
def update_risk_state(key_id: int, level: int, score: int, reasons: list) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE api_keys
               SET risk_level = ?, risk_score = ?, risk_reasons = ?,
                   risk_updated_at = datetime('now')
               WHERE id = ?""",
            (level, score, json.dumps(reasons), key_id),
        )


def set_frozen(key_id: int, frozen: bool, reason: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET risk_frozen = ?, frozen_reason = ? WHERE id = ?",
            (1 if frozen else 0, reason, key_id),
        )


def set_force_approval_until(key_id: int, until_iso: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET force_approval_until = ? WHERE id = ?",
            (until_iso, key_id),
        )


def set_daily_cap_halved_until(key_id: int, until_iso: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET daily_cap_halved_until = ? WHERE id = ?",
            (until_iso, key_id),
        )


def set_risk_override(key_id: int, override: dict | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET risk_override_json = ? WHERE id = ?",
            (json.dumps(override) if override else None, key_id),
        )


def set_requested_monthly_cap(key_id: int, cents: int | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE api_keys SET requested_monthly_cap_cents = ? WHERE id = ?",
            (cents, key_id),
        )


def insert_risk_event(key_id: int, kind: str, detail: dict | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO risk_events (api_key_id, kind, detail) VALUES (?, ?, ?)",
            (key_id, kind,
             json.dumps(detail) if detail is not None else None),
        )
        return cur.lastrowid


def count_risk_events(key_id: int, kind: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM risk_events "
            "WHERE api_key_id = ? AND kind = ?",
            (key_id, kind),
        ).fetchone()
        return int(row["n"])


def latest_risk_event_at(key_id: int, kind: str):
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS ts FROM risk_events "
            "WHERE api_key_id = ? AND kind = ?",
            (key_id, kind),
        ).fetchone()
        return row["ts"]


def count_clean_sends(key_id: int) -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM send_requests s
               JOIN drafts d ON d.id = s.draft_id
               WHERE d.api_key_id = ? AND s.status = 'SENT'""",
            (key_id,),
        ).fetchone()
        return int(row["n"])


def get_key_id_by_stripe_payment_id(payment_intent_id: str):
    """Find the API key behind a Stripe payment intent id (for dispute
    webhooks): receipts store the PI id at authorize/capture time."""
    with connect() as conn:
        row = conn.execute(
            "SELECT api_key_id FROM billing_receipts "
            "WHERE stripe_payment_id = ? ORDER BY id DESC LIMIT 1",
            (payment_intent_id,),
        ).fetchone()
        return row["api_key_id"] if row else None


# ---- drafts ---------------------------------------------------------------
def insert_draft(api_key_id, to, from_addr, html, color, quantity, description,
                 quote, verification, pdf_path=None, page_count=None,
                 needs_review=False, certified=False) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO drafts
               (api_key_id, to_json, from_json, html, color, quantity, description,
                quote_json, verification_json, pdf_path, page_count, needs_review,
                certified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                api_key_id,
                json.dumps(to),
                json.dumps(from_addr),
                html,
                1 if color else 0,
                quantity,
                description,
                json.dumps(quote),
                json.dumps(verification) if verification else None,
                pdf_path,
                page_count,
                1 if needs_review else 0,
                1 if certified else 0,
            ),
        )
        return cur.lastrowid


def get_draft(draft_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def set_draft_pdf(draft_id: int, pdf_path: str, page_count: int,
                  needs_review: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE drafts SET pdf_path = ?, page_count = ?, needs_review = ? "
            "WHERE id = ?",
            (pdf_path, page_count, 1 if needs_review else 0, draft_id),
        )


# ---- send requests --------------------------------------------------------
def insert_send_request(draft_id: int, amount_cents: int | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO send_requests (draft_id, status, amount_cents) "
            "VALUES (?, 'PENDING', ?)",
            (draft_id, amount_cents),
        )
        return cur.lastrowid


def get_send_request(request_id: int):
    with connect() as conn:
        return conn.execute(
            """SELECT s.*, d.to_json, d.from_json, d.html, d.color, d.quantity,
                      d.description, d.quote_json, d.api_key_id, d.pdf_path,
                      d.page_count, d.needs_review, d.certified
               FROM send_requests s JOIN drafts d ON d.id = s.draft_id
               WHERE s.id = ?""",
            (request_id,),
        ).fetchone()


def list_send_requests(status: str | None = None):
    with connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT s.*, d.quote_json, d.quantity, d.pdf_path,
                          substr(d.html, 1, 2000) AS html_preview
                   FROM send_requests s
                   JOIN drafts d ON d.id = s.draft_id
                   WHERE s.status = ? ORDER BY s.id DESC""",
                (status.upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT s.*, d.quote_json, d.quantity, d.pdf_path,
                          substr(d.html, 1, 2000) AS html_preview
                   FROM send_requests s
                   JOIN drafts d ON d.id = s.draft_id
                   ORDER BY s.id DESC"""
            ).fetchall()
        return rows


def list_send_requests_for_key(key_id: int, status: str | None = None):
    """Key-scoped send listing for the customer dashboard: only rows whose
    draft belongs to this API key, newest first. Never leaks other keys' data."""
    with connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT s.*, d.quote_json, d.quantity, d.pdf_path,
                          d.certified, d.to_json
                   FROM send_requests s
                   JOIN drafts d ON d.id = s.draft_id
                   WHERE d.api_key_id = ? AND s.status = ?
                   ORDER BY s.id DESC""",
                (key_id, status.upper()),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT s.*, d.quote_json, d.quantity, d.pdf_path,
                          d.certified, d.to_json
                   FROM send_requests s
                   JOIN drafts d ON d.id = s.draft_id
                   WHERE d.api_key_id = ?
                   ORDER BY s.id DESC""",
                (key_id,),
            ).fetchall()
        return rows


def update_send_request(request_id: int, **fields) -> None:
    allowed = {"status", "provider_letter_id", "failure_reason", "approved_by",
               "decided_at", "amount_cents", "tier_decision"}
    sets = [f"{k} = ?" for k in fields if k in allowed]
    if not sets:
        return
    with connect() as conn:
        conn.execute(
            f"UPDATE send_requests SET {', '.join(sets)} WHERE id = ?",
            [fields[k] for k in fields if k in allowed] + [request_id],
        )


# ---- spend / anomaly measurements ------------------------------------------
def spend_today_cents(api_key_id: int) -> int:
    """Committed spend today: sum of amount_cents for SENT requests decided today."""
    with connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(s.amount_cents), 0) AS total
               FROM send_requests s JOIN drafts d ON d.id = s.draft_id
               WHERE d.api_key_id = ? AND s.status = 'SENT'
                 AND date(s.decided_at) = date('now')""",
            (api_key_id,),
        ).fetchone()
        return int(row["total"] or 0)


def spend_month_cents(api_key_id: int) -> int:
    """Committed spend in the current calendar month: sum of amount_cents
    for SENT requests decided this month. Backs the hard monthly cap."""
    with connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(s.amount_cents), 0) AS total
               FROM send_requests s JOIN drafts d ON d.id = s.draft_id
               WHERE d.api_key_id = ? AND s.status = 'SENT'
                 AND strftime('%Y-%m', s.decided_at) = strftime('%Y-%m', 'now')""",
            (api_key_id,),
        ).fetchone()
        return int(row["total"] or 0)


def sends_today_count(api_key_id: int) -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM send_requests s
               JOIN drafts d ON d.id = s.draft_id
               WHERE d.api_key_id = ? AND date(s.created_at) = date('now')""",
            (api_key_id,),
        ).fetchone()
        return int(row["n"])


def avg_daily_sends_7d(api_key_id: int) -> float:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM send_requests s
               JOIN drafts d ON d.id = s.draft_id
               WHERE d.api_key_id = ?
                 AND s.created_at >= datetime('now', '-7 days')""",
            (api_key_id,),
        ).fetchone()
        return float(row["n"]) / 7.0


# ---- billing receipts --------------------------------------------------------
def insert_receipt(send_request_id: int, api_key_id: int, amount_cents: int,
                   currency: str, stripe_payment_id: str | None, status: str,
                   idempotency_key: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO billing_receipts
               (send_request_id, api_key_id, amount_cents, currency,
                stripe_payment_id, status, idempotency_key)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (send_request_id, api_key_id, amount_cents, currency,
             stripe_payment_id, status, idempotency_key),
        )
        return cur.lastrowid


def get_receipt_by_idempotency(idempotency_key: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM billing_receipts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()


def get_receipts_for_request(request_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM billing_receipts WHERE send_request_id = ? "
            "ORDER BY id DESC",
            (request_id,),
        ).fetchall()


# ---- approval tokens (single-use Allow/Deny links) ---------------------------
def create_approval_token(token_hash: str, request_id: int, expires_at: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO approval_tokens
               (token_hash, request_id, expires_at, used)
               VALUES (?, ?, ?, 0)""",
            (token_hash, request_id, expires_at),
        )


def consume_approval_token(token_hash: str, now: str):
    """Atomically consume an unused, unexpired token. Returns the row or None."""
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM approval_tokens
               WHERE token_hash = ? AND used = 0 AND expires_at > ?""",
            (token_hash, now),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE approval_tokens SET used = 1 WHERE token_hash = ? AND used = 0",
            (token_hash,),
        )
        return row


def get_active_token_for_request(request_id: int, now: str):
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM approval_tokens
               WHERE request_id = ? AND used = 0 AND expires_at > ?
               ORDER BY id DESC LIMIT 1""",
            (request_id, now),
        ).fetchone()


# ---- connect sessions (one-time rail authorization) --------------------------
def create_connect_session(token_hash: str, name: str, owner_email: str,
                           monthly_cap_cents: int, stripe_customer_id: str,
                           expires_at: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO connect_sessions
               (token_hash, name, owner_email, monthly_cap_cents,
                stripe_customer_id, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (token_hash, name, owner_email, monthly_cap_cents,
             stripe_customer_id, expires_at),
        )
        return cur.lastrowid


def get_connect_session(token_hash: str, now: str):
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM connect_sessions
               WHERE token_hash = ? AND status = 'pending' AND expires_at > ?""",
            (token_hash, now),
        ).fetchone()


def complete_connect_session(session_id: int, api_key_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE connect_sessions SET status = 'completed', api_key_id = ? "
            "WHERE id = ?",
            (api_key_id, session_id),
        )


# ---- audit ----------------------------------------------------------------
def audit(actor: str, action: str, target_type=None, target_id=None, detail=None) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO audit_log (actor, action, target_type, target_id, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (
                actor,
                action,
                target_type,
                target_id,
                json.dumps(detail) if not isinstance(detail, str) else detail,
            ),
        )


def recent_audit(limit: int = 200):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
