"""PVWAP strategy — NIFTY only (replaces C1-C4 for this instrument).

Locks a daily CE/PE bias pre-market using multi-timeframe swing/zone/
Fibonacci structure analysis, then mechanically trades VWAP crossovers on
NIFTY futures intraday, filtered by RSI slope, with SL-only exits.

Named "PVWAP" (not "C5") to avoid colliding with the existing Supertrend
condition already called C5 in src/signals.py.

Bias-determination heuristic (spec §3 gives the function signature and
result shape but not the exact decision rule — this is the interpretation
implemented here, using the single pre-market open price as a proxy since
no intraday pre-market path is available):
  - "trap": pre-market gapped DOWN from the previous close but is sitting
    at/above the previous support zone (within touch tolerance) — reads as
    a down-gap trapped right at support, likely to reverse up (CE).
  - "rejection": pre-market gapped UP from the previous close but is
    sitting at/below a resistance zone (within touch tolerance) — reads as
    an up-gap rejected right at resistance, likely to reverse down (PE).
  - "neutral": neither pattern is present, or there are no validated zones.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from src import config, notifier, state

IST = ZoneInfo("Asia/Kolkata")

_PREMARKET_FETCH_RETRIES = 3
_PREMARKET_RETRY_BACKOFF_SECONDS = 3


# ── Swing detection & zone validation ───────────────────────────────────────

def detect_swings(df: pd.DataFrame, window: int = 5) -> list[dict]:
    """Fractal swing points: a candle is a swing high/low if its high/low is
    the strict extreme over `window` candles on both sides. Returns a
    chronological list of {index, price, type: "high"|"low", timestamp}."""
    swings: list[dict] = []
    n = len(df)
    for i in range(window, n - window):
        hi = df["high"].iloc[i]
        lo = df["low"].iloc[i]
        left_hi  = df["high"].iloc[i - window:i]
        right_hi = df["high"].iloc[i + 1:i + 1 + window]
        left_lo  = df["low"].iloc[i - window:i]
        right_lo = df["low"].iloc[i + 1:i + 1 + window]

        if hi > left_hi.max() and hi > right_hi.max():
            swings.append({
                "index": i, "price": float(hi), "type": "high",
                "timestamp": df["timestamp"].iloc[i],
            })
        if lo < left_lo.min() and lo < right_lo.min():
            swings.append({
                "index": i, "price": float(lo), "type": "low",
                "timestamp": df["timestamp"].iloc[i],
            })
    return swings


def validate_zones(swings: list[dict], tolerance_pct: float = 0.15,
                    min_touches: int = 2) -> list[dict]:
    """Cluster swings (separately for highs/resistance and lows/support)
    within tolerance_pct of each other's price. A cluster qualifies as a
    Zone only if it has >= min_touches swings. Empty list is valid/expected
    (choppy lookback window)."""
    zones: list[dict] = []
    for swing_type, zone_type in (("high", "resistance"), ("low", "support")):
        prices = sorted(s["price"] for s in swings if s["type"] == swing_type)
        cluster: list[float] = []
        for price in prices:
            if cluster and abs(price - cluster[-1]) / cluster[-1] * 100 > tolerance_pct:
                if len(cluster) >= min_touches:
                    zones.append({
                        "level": round(sum(cluster) / len(cluster), 2),
                        "type": zone_type,
                        "touches": len(cluster),
                    })
                cluster = []
            cluster.append(price)
        if len(cluster) >= min_touches:
            zones.append({
                "level": round(sum(cluster) / len(cluster), 2),
                "type": zone_type,
                "touches": len(cluster),
            })
    return zones


# ── Fibonacci levels ────────────────────────────────────────────────────────

def fibonacci_levels(swing_high: float, swing_low: float) -> dict:
    """Retracement levels of the most recent major swing on the 15-min
    series. Returns {"0.382": ..., "0.500": ..., "0.618": ...}."""
    rng = swing_high - swing_low
    return {
        "0.382": round(swing_high - 0.382 * rng, 2),
        "0.500": round(swing_high - 0.500 * rng, 2),
        "0.618": round(swing_high - 0.618 * rng, 2),
    }


# ── Bias determination ──────────────────────────────────────────────────────

def determine_bias(previous_close: float, previous_support: float | None,
                    premarket_open: float, zones: list[dict],
                    fib_levels: dict) -> dict:
    """Returns {"bias": "CE"|"PE"|"NEUTRAL", "rationale": str,
    "support": float|None, "resistance": float|None}. See module docstring
    for the heuristic. Zero valid zones -> NEUTRAL (confirmed edge case)."""
    if not zones:
        return {"bias": "NEUTRAL", "rationale": "no_valid_zones",
                "support": None, "resistance": None}

    supports = [z["level"] for z in zones if z["type"] == "support"]
    resistances = [z["level"] for z in zones if z["type"] == "resistance"]

    support = max((s for s in supports if s <= premarket_open), default=None)
    if support is None and supports:
        support = max(supports)
    resistance = min((r for r in resistances if r >= premarket_open), default=None)
    if resistance is None and resistances:
        resistance = min(resistances)

    tolerance = config.PVWAP_TOUCH_TOLERANCE_PCT / 100.0

    # Trap: gapped down from the previous close, but sitting at/above
    # previous support (within tolerance) — down-gap trapped at support.
    if (previous_support is not None
            and premarket_open < previous_close
            and previous_support <= premarket_open <= previous_support * (1 + tolerance)):
        return {"bias": "CE", "rationale": "trap",
                "support": support, "resistance": resistance}

    # Rejection: gapped up from the previous close, but sitting at/below
    # a resistance zone (within tolerance) — up-gap rejected at resistance.
    if (resistance is not None
            and premarket_open > previous_close
            and resistance * (1 - tolerance) <= premarket_open <= resistance):
        return {"bias": "PE", "rationale": "rejection",
                "support": support, "resistance": resistance}

    return {"bias": "NEUTRAL", "rationale": "neutral",
            "support": support, "resistance": resistance}


# ── Intraday entry check ────────────────────────────────────────────────────

def check_entry(bias: str, live_ltp: float, live_vwap: float,
                 live_rsi: float, prev_rsi: float,
                 position_open: bool) -> bool:
    if position_open or bias == "NEUTRAL":
        return False
    if bias == "CE":
        return live_ltp > live_vwap and live_rsi > prev_rsi
    if bias == "PE":
        return live_ltp < live_vwap and live_rsi < prev_rsi
    return False


# ── Stop-loss calculation ───────────────────────────────────────────────────

def compute_sl(df_5m: pd.DataFrame, bias: str, candles: int = 5) -> float:
    """CE: min(low) of the last `candles` 5-min candles.
    PE: max(high) of the last `candles` 5-min candles.
    No resting target order is ever derived here."""
    tail = df_5m.tail(candles)
    if bias == "CE":
        return float(tail["low"].min())
    return float(tail["high"].max())


# ── Redis caching ───────────────────────────────────────────────────────────

def _bias_key(date_str: str) -> str:
    return f"pvwap:bias:{date_str}"


def _zones_key(date_str: str) -> str:
    return f"pvwap:zones:{date_str}"


def _open_position_key(date_str: str) -> str:
    return f"pvwap:open_position:{date_str}"


def _seconds_until_2359_ist(now: datetime) -> int:
    target = now.replace(hour=23, minute=59, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(int((target - now).total_seconds()), 60)


def get_cached_bias(date_str: str) -> dict | None:
    raw = state.redis_get(_bias_key(date_str))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _cache_bias(date_str: str, bias_data: dict, now: datetime) -> None:
    ttl = _seconds_until_2359_ist(now)
    state.redis_set(_bias_key(date_str), json.dumps(bias_data), ex=ttl)


def _cache_zones(date_str: str, zones: list[dict], fib_levels: dict, now: datetime) -> None:
    ttl = _seconds_until_2359_ist(now)
    payload = {"zones": zones, "fib_levels": fib_levels}
    state.redis_set(_zones_key(date_str), json.dumps(payload), ex=ttl)


def mark_open_position(date_str: str, tradingsymbol: str, now: datetime) -> None:
    """Record the tradingsymbol of the paper position PVWAP just opened.
    Keyed separately from position_tracker.py's generic state (§position
    permits re-entry semantics the generic tracker doesn't model) and,
    since NIFTY can now also run the generic C1-C4 path concurrently, an
    open NIFTY paper position no longer implies it's a PVWAP position — the
    flag must track PVWAP's own tradingsymbol specifically, not just
    "any open NIFTY position" (see is_position_open)."""
    ttl = _seconds_until_2359_ist(now)
    state.redis_set(_open_position_key(date_str), json.dumps({"tradingsymbol": tradingsymbol}), ex=ttl)


def is_position_open(date_str: str) -> bool:
    """True if PVWAP's own tracked position is still open. Self-healing:
    since the frozen paper_engine.py exit path has no PVWAP-awareness and
    will never clear this flag directly, this checks the *specific*
    tradingsymbol PVWAP opened against paper_engine's live position state
    on every call, and clears the flag itself once that position is gone."""
    raw = state.redis_get(_open_position_key(date_str))
    if not raw:
        return False
    try:
        tradingsymbol = json.loads(raw).get("tradingsymbol")
    except Exception:
        state.redis_delete(_open_position_key(date_str))
        return False

    from src import paper_engine
    if tradingsymbol and paper_engine.load_paper_position(tradingsymbol):
        return True

    state.redis_delete(_open_position_key(date_str))
    return False


# ── Pre-market orchestration ────────────────────────────────────────────────

def _nifty_futures_token() -> int | None:
    raw = state.redis_get("kite:instrument_tokens")
    if not raw:
        return None
    try:
        tokens = json.loads(raw)
    except Exception:
        return None
    info = tokens.get("NIFTY")
    return info["token"] if info else None


def _fetch_with_retry(token: int, interval: str, lookback_days: int) -> pd.DataFrame | None:
    from src import kite_client
    for attempt in range(1, _PREMARKET_FETCH_RETRIES + 1):
        try:
            return kite_client.fetch_ohlcv_multi(token, interval, lookback_days)
        except Exception as e:
            print(f"[pvwap_signals] {interval} fetch attempt {attempt}/"
                  f"{_PREMARKET_FETCH_RETRIES} failed: {e}")
            if attempt < _PREMARKET_FETCH_RETRIES:
                time.sleep(_PREMARKET_RETRY_BACKOFF_SECONDS * attempt)
    return None


def run_premarket() -> dict:
    """Computes the daily PVWAP bias for NIFTY, caches it (+ zones) in Redis,
    and fires the standalone pre-market Discord alert. Never raises — on
    total data-fetch failure after 3 retries, falls back to a NEUTRAL bias
    with rationale "premarket_fetch_failed" so NIFTY is safely skipped for
    the day rather than crashing morning-login.yml."""
    now = datetime.now(IST)
    date_str = now.date().isoformat()

    token = _nifty_futures_token()
    df_1h = _fetch_with_retry(token, "60minute", config.PVWAP_LOOKBACK_DAYS) if token else None
    df_15m = _fetch_with_retry(token, "15minute", config.PVWAP_LOOKBACK_DAYS) if token else None

    if token is None or df_1h is None or df_15m is None or len(df_1h) < config.PVWAP_FRACTAL_WINDOW * 2 + 1:
        bias_data = {
            "bias": "NEUTRAL", "rationale": "premarket_fetch_failed",
            "support": None, "resistance": None,
            "computed_at": now.isoformat(),
        }
        _cache_bias(date_str, bias_data, now)
        _cache_zones(date_str, [], {}, now)
        notifier.send_pvwap_bias(date_str, bias_data, {})
        return bias_data

    from src import kite_client
    premarket_open = kite_client.get_spot_ltp("NIFTY")
    if premarket_open is None:
        premarket_open = float(df_1h["close"].iloc[-1])

    previous_close = float(df_1h["close"].iloc[-1])

    swings_1h = detect_swings(df_1h, window=config.PVWAP_FRACTAL_WINDOW)
    zones = validate_zones(
        swings_1h,
        tolerance_pct=config.PVWAP_TOUCH_TOLERANCE_PCT,
        min_touches=config.PVWAP_MIN_TOUCHES,
    )

    supports_below_close = [z["level"] for z in zones
                             if z["type"] == "support" and z["level"] <= previous_close]
    previous_support = max(supports_below_close) if supports_below_close else None

    swings_15m = detect_swings(df_15m, window=config.PVWAP_FRACTAL_WINDOW)
    recent_highs = [s for s in swings_15m if s["type"] == "high"]
    recent_lows = [s for s in swings_15m if s["type"] == "low"]
    if recent_highs and recent_lows:
        swing_high = max(recent_highs[-1]["price"], recent_lows[-1]["price"])
        swing_low = min(recent_highs[-1]["price"], recent_lows[-1]["price"])
        fib_levels = fibonacci_levels(swing_high, swing_low)
    else:
        fib_levels = {}

    bias_result = determine_bias(previous_close, previous_support, premarket_open, zones, fib_levels)
    bias_data = {**bias_result, "computed_at": now.isoformat()}

    _cache_bias(date_str, bias_data, now)
    _cache_zones(date_str, zones, fib_levels, now)
    notifier.send_pvwap_bias(date_str, bias_data, fib_levels)
    return bias_data


if __name__ == "__main__":
    run_premarket()
