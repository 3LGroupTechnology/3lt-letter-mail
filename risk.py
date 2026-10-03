"""Risk orchestration for the letter-mail service.

Wires the pure scoring model in tlt_risk (~/workspace/3lt-shared) to this
service's SQLite store:

  * collect_stats(key_id)   -> stats dict for tlt_risk.compute_risk
  * initialize_key(key_id)  -> brand-new key starts at L0 (score 50)
  * recompute(key_id)       -> re-score after any send outcome, persist the
                               new level/score/caps; audit on change
  * record_event(...)       -> content flag / failed payment / chargeback /
                               velocity spike, with the downgrade side-effects
  * effective_caps(key_row) -> the caps actually enforced right now
                               (admin override > risk level; velocity-halving
                               and customer-requested monthly budget applied)
  * effective_require_approval(key_row) -> per-send prompt needed?
  * check_frozen(key_row)   -> (frozen, reason) for the 403 gate
  * apply_override(...)     -> admin level/caps/freeze control (audited)
  * get_risk_view(key_id)   -> everything the admin risk endpoint returns

Enforcement reads effective_caps(), never the raw row columns — the row
columns are the persisted *policy* caps (for dashboards); the 24h velocity
halving and the customer-requested monthly budget are applied at read time.

The risk engine owns ALL caps now. A customer may request a LOWER monthly
budget at Connect time (requested_monthly_cap_cents); the enforced monthly
cap is min(requested, risk_level_monthly). The engine is the ceiling, the
customer can only lower it. (Judgment call — documented.)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

_SHARED = os.environ.get("TLT_SHARED_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "3lt-shared"
)
if os.path.isdir(_SHARED) and _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

import db
import tlt_risk

# Risk event kinds recorded in the risk_events table.
EV_FLAG = "flag"
EV_FAILED_PAYMENT = "failed_payment"
EV_CHARGEBACK = "chargeback"
EV_VELOCITY_SPIKE = "velocity_spike"
VALID_EVENTS = {EV_FLAG, EV_FAILED_PAYMENT, EV_CHARGEBACK, EV_VELOCITY_SPIKE}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: str | None):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        # SQLite stores naive "YYYY-MM-DD HH:MM:SS"; treat as UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---- stats --------------------------------------------------------------------
def collect_stats(key_id: int) -> dict:
    """Build the stats dict tlt_risk.compute_risk() scores."""
    key = db.get_key(key_id)
    created = _parse_ts(key["created_at"]) if key else None
    tenure_days = (datetime.now(timezone.utc) - created).days if created else 0

    latest_flag = db.latest_risk_event_at(key_id, EV_FLAG)
    days_since_flag = None
    if latest_flag:
        dt = _parse_ts(latest_flag)
        if dt:
            days_since_flag = (datetime.now(timezone.utc) - dt).days

    return {
        "tenure_days": max(0, tenure_days),
        "clean_sends": db.count_clean_sends(key_id),
        "flagged_sends": db.count_risk_events(key_id, EV_FLAG),
        "failed_payments": db.count_risk_events(key_id, EV_FAILED_PAYMENT),
        "chargebacks": db.count_risk_events(key_id, EV_CHARGEBACK),
        "velocity_spikes": db.count_risk_events(key_id, EV_VELOCITY_SPIKE),
        "sends_last_7d_avg": db.avg_daily_sends_7d(key_id),
        "sends_today": db.sends_today_count(key_id),
        # No email-verification flow exists yet: owner_email present is NOT
        # treated as verified (documented; verification is future work).
        "email_verified": False,
        # Card signals need live Stripe payment-method enrichment; in test
        # mode they stay neutral (documented future work).
        "card_avs_cvc_pass": False,
        "card_type_risk": "medium",
        "days_since_flag": days_since_flag,
    }


# ---- cap resolution ---------------------------------------------------------------
def _override_of(key_row) -> dict | None:
    raw = key_row["risk_override_json"] if "risk_override_json" in key_row.keys() else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _policy_caps(key_row, level: int) -> dict:
    """Policy caps for a level, honoring an admin override if present.
    A partial caps override merges over the level's table — unspecified
    fields stay at the level's values, never zero."""
    ov = _override_of(key_row)
    if ov and "level" in ov:
        base = dict(tlt_risk.caps_for_level(int(ov["level"])))
    else:
        base = dict(tlt_risk.caps_for_level(level))
    if ov and isinstance(ov.get("caps"), dict):
        for k in ("monthly_cents", "daily_cents", "per_send_cents"):
            if ov["caps"].get(k) is not None:
                base[k] = int(ov["caps"][k])
    return base


def effective_caps(key_row) -> dict:
    """The caps actually enforced right now for this key row."""
    level = key_row["risk_level"] if "risk_level" in key_row.keys() else 0
    base = _policy_caps(key_row, level)
    requested = (key_row["requested_monthly_cap_cents"]
                 if "requested_monthly_cap_cents" in key_row.keys() else None)
    monthly = base["monthly_cents"]
    if requested:
        monthly = min(monthly, int(requested))
    daily = base["daily_cents"]
    halved_until = _parse_ts(
        key_row["daily_cap_halved_until"]
        if "daily_cap_halved_until" in key_row.keys() else None)
    if halved_until and datetime.now(timezone.utc) < halved_until:
        daily = daily // 2
    return {"monthly_cents": monthly, "daily_cents": daily,
            "per_send_cents": base["per_send_cents"]}


def persist_policy_caps(key_id: int) -> dict:
    """Write the (override-aware, pre-halving) policy caps to the row so
    dashboards and legacy readers see current values. Returns them."""
    key_row = db.get_key(key_id)
    level = key_row["risk_level"]
    base = _policy_caps(key_row, level)
    requested = key_row["requested_monthly_cap_cents"]
    monthly = min(base["monthly_cents"], int(requested)) if requested \
        else base["monthly_cents"]
    db.set_key_caps(key_id, daily_cap_cents=base["daily_cents"],
                    per_send_cap_cents=base["per_send_cents"],
                    monthly_cap_cents=monthly)
    return {"monthly_cents": monthly, "daily_cents": base["daily_cents"],
            "per_send_cents": base["per_send_cents"]}


def effective_require_approval(key_row) -> bool:
    """True if this send needs the per-send Allow/Deny prompt."""
    if bool(key_row["require_per_send_approval"]):
        return True
    until = _parse_ts(key_row["force_approval_until"]
                      if "force_approval_until" in key_row.keys() else None)
    return bool(until and datetime.now(timezone.utc) < until)


def check_frozen(key_row) -> tuple[bool, str | None]:
    """Return (frozen, reason). Frozen keys get a 403 on sends."""
    if key_row["risk_frozen"] if "risk_frozen" in key_row.keys() else 0:
        reason = (key_row["frozen_reason"]
                  if "frozen_reason" in key_row.keys() else None)
        return True, reason or "key frozen by risk engine"
    return False, None


# ---- lifecycle ----------------------------------------------------------------------
def initialize_key(key_id: int, requested_monthly_cap_cents: int | None = None,
                   actor: str = "system") -> dict:
    """Brand-new key: score 50, level 0, L0 caps. Called at key creation
    (both the Connect flow and admin-issued keys)."""
    if requested_monthly_cap_cents:
        db.set_requested_monthly_cap(key_id, requested_monthly_cap_cents)
    return recompute(key_id, actor=actor, event_note="key created")


def recompute(key_id: int, actor: str = "system", pin_level: int | None = None,
              event_note: str | None = None) -> dict:
    """Re-score the key, persist level/score/caps, audit on change.

    pin_level: force the stored level (used by the flag demotion, which drops
    exactly one level rather than re-deriving). Otherwise the level is
    derived from score + history gates.
    """
    key_row = db.get_key(key_id)
    stats = collect_stats(key_id)
    result = tlt_risk.compute_risk(stats)
    level = pin_level if pin_level is not None else result["level"]

    old_level = key_row["risk_level"]
    old_caps = {"monthly_cents": key_row["monthly_cap_cents"],
                "daily_cents": key_row["daily_cap_cents"],
                "per_send_cents": key_row["per_send_cap_cents"]}

    db.update_risk_state(key_id, level, result["score"], result["reasons"])
    new_caps = persist_policy_caps(key_id)

    changed = (level != old_level or new_caps != old_caps)
    if changed or event_note:
        db.audit(actor, "risk.recomputed", "api_key", key_id,
                 {"score": result["score"], "level": level,
                  "previous_level": old_level, "caps_cents": new_caps,
                  "reasons": result["reasons"],
                  **({"note": event_note} if event_note else {})})
    return {"score": result["score"], "level": level, "caps": new_caps,
            "reasons": result["reasons"], "changed": changed}


def record_event(key_id: int, kind: str, detail: dict | None = None,
                 actor: str = "system") -> dict:
    """Record a risk event and apply its downgrade side-effects.

    kind: flag | failed_payment | chargeback | velocity_spike.
      flag           -> demote exactly 1 level (floor L0) + force per-send
                        human approval for FLAG_FORCE_APPROVAL_DAYS.
      failed_payment -> freeze sends until an admin unfreezes.
      chargeback     -> freeze sends; admin review required to unfreeze.
      velocity_spike -> daily cap halved for VELOCITY_HALVE_HOURS + audit alert.
    """
    if kind not in VALID_EVENTS:
        raise ValueError(f"unknown risk event kind: {kind!r}")
    detail = detail or {}
    key_row = db.get_key(key_id)
    db.insert_risk_event(key_id, kind, detail)

    if kind == EV_FLAG:
        new_level = tlt_risk.demote_on_flag(key_row["risk_level"])
        until = (datetime.now(timezone.utc)
                 + timedelta(days=tlt_risk.FLAG_FORCE_APPROVAL_DAYS)).isoformat()
        db.set_force_approval_until(key_id, until)
        result = recompute(key_id, actor=actor, pin_level=new_level,
                           event_note=f"content flag: demoted to L{new_level}, "
                           f"per-send approval forced for "
                           f"{tlt_risk.FLAG_FORCE_APPROVAL_DAYS}d")
        db.audit(actor, "risk.flag", "api_key", key_id,
                 {"demoted_to": new_level,
                  "force_approval_until": until, **detail})
        return result

    if kind == EV_FAILED_PAYMENT:
        result = recompute(key_id, actor=actor,
                           event_note="failed payment: sends frozen")
        db.set_frozen(key_id, True,
                      f"failed payment ({detail.get('reason', 'unspecified')}) — "
                      "unfreeze via admin risk override once resolved")
        db.audit(actor, "risk.frozen", "api_key", key_id,
                 {"reason": "failed_payment", **detail})
        result["frozen"] = True
        return result

    if kind == EV_CHARGEBACK:
        result = recompute(key_id, actor=actor,
                           event_note="chargeback: sends frozen, admin review "
                           "required")
        db.set_frozen(key_id, True,
                      "chargeback/dispute — admin review required to unfreeze")
        db.audit(actor, "risk.frozen", "api_key", key_id,
                 {"reason": "chargeback", **detail})
        result["frozen"] = True
        return result

    # velocity_spike
    until = (datetime.now(timezone.utc)
             + timedelta(hours=tlt_risk.VELOCITY_HALVE_HOURS)).isoformat()
    db.set_daily_cap_halved_until(key_id, until)
    result = recompute(key_id, actor=actor,
                       event_note=f"velocity spike: daily cap halved for "
                       f"{tlt_risk.VELOCITY_HALVE_HOURS}h")
    db.audit(actor, "risk.velocity_spike", "api_key", key_id,
             {"daily_cap_halved_until": until,
              "alert": "send volume spiked — possible compromised key or "
                       "runaway agent; daily cap halved for 24h", **detail})
    return result


# ---- admin ----------------------------------------------------------------------------
def get_risk_view(key_id: int) -> dict:
    key_row = db.get_key(key_id)
    if not key_row:
        raise KeyError(f"API key {key_id} not found")
    stats = collect_stats(key_id)
    frozen, frozen_reason = check_frozen(key_row)
    return {
        "id": key_id,
        "name": key_row["name"],
        "tier": key_row["tier"],
        "risk_score": key_row["risk_score"],
        "risk_level": key_row["risk_level"],
        "risk_reasons": json.loads(key_row["risk_reasons"] or "[]"),
        "risk_updated_at": key_row["risk_updated_at"],
        "level_caps_cents": tlt_risk.caps_for_level(key_row["risk_level"]),
        "effective_caps_cents": effective_caps(key_row),
        "persisted_caps_cents": {
            "monthly_cents": key_row["monthly_cap_cents"],
            "daily_cents": key_row["daily_cap_cents"],
            "per_send_cents": key_row["per_send_cap_cents"],
        },
        "requested_monthly_cap_cents": key_row["requested_monthly_cap_cents"],
        "frozen": frozen,
        "frozen_reason": frozen_reason,
        "force_per_send_approval_until": key_row["force_approval_until"],
        "daily_cap_halved_until": key_row["daily_cap_halved_until"],
        "override": _override_of(key_row),
        "stats": {k: v for k, v in stats.items()},
    }


def apply_override(key_id: int, payload: dict, actor: str = "admin") -> dict:
    """Admin override. payload may contain:
      level: 0-3            -> enforce caps for this level (score still
                               recomputes for information)
      caps: {monthly_cents?, daily_cents?, per_send_cents?}
                            -> enforce these exact caps
      frozen: true|false    -> freeze / unfreeze the key (+ optional reason)
      clear: true           -> drop the override, return to risk-derived caps
    Logged with reasons=["admin override"]."""
    key_row = db.get_key(key_id)
    if not key_row:
        raise KeyError(f"API key {key_id} not found")
    payload = payload or {}

    if "frozen" in payload:
        if payload["frozen"]:
            db.set_frozen(key_id, True,
                          payload.get("reason") or "frozen by admin override")
        else:
            db.set_frozen(key_id, False, None)
        db.audit(actor, "risk.override", "api_key", key_id,
                 {"frozen": bool(payload["frozen"]),
                  "reasons": ["admin override"]})

    override = _override_of(key_row) or {}
    if payload.get("clear"):
        override = {}
    else:
        if "level" in payload:
            lvl = int(payload["level"])
            if lvl not in tlt_risk.CAPS_BY_LEVEL:
                raise ValueError(f"unknown risk level: {payload['level']!r}")
            override = {"level": lvl}
        if "caps" in payload:
            c = payload["caps"] or {}
            provided = {}
            for k in ("monthly_cents", "daily_cents", "per_send_cents"):
                if c.get(k) is not None:
                    v = int(c[k])
                    if v < 0:
                        raise ValueError(f"caps.{k} must be >= 0")
                    provided[k] = v
            # An empty caps object is a no-op, not an override-clear.
            if provided:
                override = {"caps": provided}
    db.set_risk_override(key_id, override or None)
    new_caps = persist_policy_caps(key_id)
    # Keep the stored level honest when the admin pins one.
    if override.get("level") is not None:
        db.update_risk_state(key_id, override["level"], key_row["risk_score"],
                             json.loads(key_row["risk_reasons"] or "[]"))
    db.audit(actor, "risk.override", "api_key", key_id,
             {"override": override or None, "caps_cents": new_caps,
              "reasons": ["admin override"]})
    return get_risk_view(key_id)
