"""Writes a per-instrument trade intent to Redis for position_tracker.py to
consume — decoupled from executor_bridge.py (that channel is a safety
boundary for a separate, external, real-money auto-executor and must not be
touched or reused here)."""
import json
from datetime import datetime, timezone

from src import state

INTENT_KEY_PREFIX = "tracker:pending_intent:"
# Deliberately longer than executor_bridge.INTENT_TTL_SECONDS (360s) — that
# channel assumes a fast automated executor; this channel assumes a human
# reads a Discord alert and manually places the order, which can take
# several minutes.
INTENT_TTL_SECONDS = 1800  # 30 minutes


def _intent_key(instrument: str) -> str:
    return f"{INTENT_KEY_PREFIX}{instrument.upper()}"


def write_tracker_intent(
    *,
    instrument: str,
    asset_class: str,       # "INDEX" | "STOCK"
    direction: str,         # "CE" | "PE"
    tradingsymbol: str | None,
    spot_sl: float | None,
    target_pts: float | None,   # unified T — already correct for both RR-based (index) and ATR-based (stock) targets
    spot_risk_pts: float | None = None,   # optional, debugging/logging only
    target_rr: float | None = None,       # optional, INDEX only, debugging/logging only
    target_source: str | None = None,     # "rr" | "atr" | "fallback_1.5R" — informational
    atm_strike=None,
) -> bool:
    """Write the tracker-intent payload for one instrument. Never raises —
    callers wrap in try/except anyway, but this degrades gracefully itself."""
    if not tradingsymbol:
        print(f"[tracker_bridge] {instrument}: tradingsymbol missing — skipping intent write")
        return False
    if target_pts is None or target_pts <= 0:
        print(f"[tracker_bridge] {instrument}: target_pts missing/invalid ({target_pts!r}) — skipping intent write")
        return False

    payload = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "instrument":    instrument.upper(),
        "asset_class":   asset_class,
        "direction":     direction.upper(),
        "tradingsymbol": tradingsymbol,
        "spot_sl":       round(spot_sl, 2) if spot_sl is not None else None,
        "target_pts":    round(target_pts, 2),
        "spot_risk_pts": round(spot_risk_pts, 2) if spot_risk_pts is not None else None,
        "target_rr":     target_rr,
        "target_source": target_source,
        "atm_strike":    atm_strike,
    }

    ok = state.redis_set(_intent_key(instrument), json.dumps(payload), ex=INTENT_TTL_SECONDS)
    if ok:
        print(f"[tracker_bridge] Intent written: {instrument} {direction} T={payload['target_pts']}")
    else:
        print(f"[tracker_bridge] Failed to write tracker intent for {instrument}")
    return ok
