"""Writes a trade intent to Redis for the auto-executor to consume."""
import json
import os
import requests
from datetime import datetime, timezone

from . import config


REDIS_URL    = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN  = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
INTENT_KEY   = "executor:pending_intent"
POSITION_KEY = "executor:position"
INTENT_TTL_SECONDS = 360   # 6 minutes — stale after that


def _redis_get(key: str):
    resp = requests.get(
        f"{REDIS_URL}/get/{key}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
        timeout=5,
    )
    data = resp.json()
    return data.get("result")


def _redis_setex(key: str, ttl: int, value: str):
    resp = requests.post(
        f"{REDIS_URL}/setex/{key}/{ttl}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps(value),
        timeout=5,
    )
    return resp.status_code == 200


def write_executor_intent(signal_result: dict, instrument_cfg: dict) -> bool:
    """
    Writes a pending trade intent to Redis if no position is currently open.
    Returns True if written, False if skipped or failed.
    """
    instrument = instrument_cfg.get("name", "NIFTY")

    # SENSEX is alert-only — the executor (repo 2) is hardcoded NIFTY/NFO-only.
    # A SENSEX intent would be mishandled, so never write one.
    if instrument == "SENSEX":
        print("[executor_bridge] SENSEX is alert-only — skipping executor intent")
        return False

    if not REDIS_URL or not REDIS_TOKEN:
        print("[executor_bridge] Redis env vars not set — skipping intent write")
        return False

    # Don't overwrite an active position
    existing_position = _redis_get(POSITION_KEY)
    if existing_position:
        print("[executor_bridge] Position already open — skipping intent write")
        return False

    # Determine direction
    ce_signal = signal_result.get("ce", {}).get("signal", False)
    pe_signal = signal_result.get("pe", {}).get("signal", False)
    if not ce_signal and not pe_signal:
        return False
    direction = "CE" if ce_signal else "PE"

    futures_price    = signal_result.get("futures_price")
    prev_candle_low  = signal_result.get("prev_candle_low")
    prev_candle_high = signal_result.get("prev_candle_high")
    spot_ltp_val     = signal_result.get("spot_ltp")
    atm_strike       = signal_result.get("atm_strike")
    instrument       = instrument_cfg.get("name", "NIFTY")
    strike_step      = instrument_cfg.get("strike_step", 50)
    tradingsymbol    = signal_result.get("atm_data", {}).get("tradingsymbol")

    if futures_price is None or prev_candle_low is None or prev_candle_high is None:
        print("[executor_bridge] Missing price data — skipping intent write")
        return False

    if not tradingsymbol:
        print("[executor_bridge] tradingsymbol missing from atm_data — skipping intent write")
        return False

    # SL = previous candle structural extreme.
    # Risk = distance from spot (or futures fallback) to that structural level.
    atm_delta  = signal_result.get("atm_delta") \
                 or float(os.environ.get("ATM_DELTA", str(config.ATM_DELTA)))
    target_rr  = config.TARGET_RR

    reference = spot_ltp_val if spot_ltp_val is not None else futures_price

    if direction == "CE":
        spot_sl       = round(prev_candle_low,  2)
        spot_risk_pts = max(reference - spot_sl, 0.5)
    else:
        spot_sl       = round(prev_candle_high, 2)
        spot_risk_pts = max(spot_sl - reference, 0.5)

    intent = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "instrument":    instrument,
        "direction":     direction,
        "tradingsymbol": tradingsymbol,
        "atm_strike":    atm_strike,
        "spot_close":    round(reference, 2),
        "spot_sl":       round(spot_sl, 2),
        "spot_risk_pts": round(spot_risk_pts, 2),
        "target_rr":     target_rr,
        "atm_delta":     atm_delta,
        "conviction":    "label_only",
        "health_inputs": {
            "vwap": signal_result.get("vwap"),
            "rsi":  signal_result.get("rsi"),
            "pdi":  signal_result.get("pdi"),
            "ndi":  signal_result.get("ndi"),
        },
    }

    success = _redis_setex(INTENT_KEY, INTENT_TTL_SECONDS, json.dumps(intent))
    if success:
        print(f"[executor_bridge] Intent written to Redis: {direction} {instrument} {atm_strike}")
    else:
        print("[executor_bridge] Failed to write intent to Redis")
    return success
