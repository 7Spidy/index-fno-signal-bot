"""
stock_kite_client.py — Stock-specific token caching for morning-login.

Two CLI entry points:
  --cache-equity-tokens   → kite:stock_equity_tokens  (NSE equity OHLCV tokens)
  --cache-stock-options   → kite:stock_option_tokens  (NFO monthly ATM option cache)

Called by morning-login.yml after the existing index caching steps.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

from src import stock_config as cfg
from src.kite_client import get_kite
from src.state import redis_set


def cache_stock_equity_tokens() -> None:
    """
    Fetch NSE instruments dump, filter for the 7 equity symbols,
    store {symbol: instrument_token} in Redis.
    Equity tokens are stable (no expiry rollover) but refreshed daily
    for safety. TTL 26 hours — survives overnight.
    """
    kite        = get_kite()
    instruments = kite.instruments("NSE")

    result = {}
    for inst in instruments:
        sym = inst.get("tradingsymbol", "")
        if sym in cfg.STOCK_EQUITY_SYMBOLS and inst.get("instrument_type") == "EQ":
            result[sym] = inst["instrument_token"]
            print(f"[stock_kite] equity token: {sym} → {inst['instrument_token']}")

    missing = cfg.STOCK_EQUITY_SYMBOLS - result.keys()
    if missing:
        print(f"[stock_kite] WARNING — equity tokens not found: {missing}")

    redis_set(cfg.REDIS_EQUITY_TOKENS_KEY, json.dumps(result), ex=93600)  # 26 h
    print(f"[stock_kite] Cached {len(result)} equity tokens → {cfg.REDIS_EQUITY_TOKENS_KEY}")


def _get_nearest_monthly_expiry(name: str, instruments: list[dict]) -> tuple[date | None, bool]:
    """
    Find the nearest monthly expiry for a stock, with rollover applied.
    Returns (expiry, rolled_forward). All stocks are monthly-only.

    Rolls to the next available expiry when the nearest is today OR the
    next NSE trading day — matching the index monthly-branch logic in
    kite_client.get_nearest_expiry(), kept deliberately independent.
    """
    from src import calendar_nse
    today = date.today()
    candidates = [
        i["expiry"] for i in instruments
        if i.get("name") == name
        and i.get("instrument_type") in ("CE", "PE")
        and i.get("expiry") and i["expiry"] >= today
    ]
    if not candidates:
        return None, False

    distinct_expiries = sorted(set(candidates))
    candidate = distinct_expiries[0]

    next_td = today + timedelta(days=1)
    while not calendar_nse.is_trading_day(next_td):
        next_td += timedelta(days=1)

    if candidate == today or candidate == next_td:
        if len(distinct_expiries) > 1:
            exp = distinct_expiries[1]
        else:
            print(f"[stock_kite] WARNING: {name} monthly rollover wanted but "
                  f"only one expiry in dump — using {candidate}")
            exp = candidate
    else:
        exp = candidate

    rolled = (exp != candidate)
    return exp, rolled


def cache_stock_option_tokens() -> None:
    """
    Fetch NFO instruments dump, cache monthly ATM option tokens for all 7 stocks.
    Key format: {NAME}_{STRIKE}_{CE/PE} → {token, tradingsymbol, lot_size, expiry}.
    Stored in kite:stock_option_tokens (separate from index kite:option_tokens).
    """
    kite        = get_kite()
    instruments = kite.instruments("NFO")
    result      = {}

    for stock in cfg.STOCKS:
        name  = stock["name"]
        step  = stock["strike_step"]
        rng   = cfg.OPTION_CACHE_RANGE[name]

        expiry, rolled = _get_nearest_monthly_expiry(name, instruments)
        if not expiry:
            print(f"[stock_kite] WARNING — no monthly expiry found for {name}")
            continue

        spot_key = f"NSE:{stock['equity_symbol']}"
        try:
            ltp_data = kite.ltp([spot_key])
            spot = ltp_data[spot_key]["last_price"]
        except Exception as e:
            print(f"[stock_kite] Could not fetch spot for {name}: {e}")
            continue

        matches = [
            i for i in instruments
            if i.get("name") == name
            and i.get("instrument_type") in ("CE", "PE")
            and i.get("expiry") == expiry
            and abs(i.get("strike", 0) - spot) <= rng
        ]

        for m in matches:
            key = f"{name}_{int(m['strike'])}_{m['instrument_type']}"
            result[key] = {
                "token":          m["instrument_token"],
                "tradingsymbol":  m["tradingsymbol"],
                "lot_size":       m["lot_size"],
                "expiry":         expiry.isoformat(),
                "rolled_forward": rolled,
            }

        print(
            f"[stock_kite] {name}: cached {len(matches)} strikes "
            f"around {spot:.0f}, expiry {expiry} (monthly)"
            + (" [rolled forward]" if rolled else "")
        )

    redis_set(cfg.REDIS_OPTION_TOKENS_KEY, json.dumps(result), ex=93600)
    print(f"[stock_kite] Total cached: {len(result)} stock option tokens → {cfg.REDIS_OPTION_TOKENS_KEY}")


if __name__ == "__main__":
    if "--cache-equity-tokens" in sys.argv:
        cache_stock_equity_tokens()
    if "--cache-stock-options" in sys.argv:
        cache_stock_option_tokens()
