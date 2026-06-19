"""
stock_events.py — NSE corporate-event exclusion list for the stock bot.

Checks Yahoo Finance's calendarEvents API for upcoming earnings and dividend
ex-dates for tracked stocks. Runs once a day from morning-login and writes
the set of symbols with an event in the next EVENT_LOOKAHEAD_DAYS calendar
days to Redis. Fails open: any failure results in an empty (or partial)
exclusion list plus a Discord warning, never a full block of the stock bot.

Replaced NSE top-corp-info scrape (blocked by Akamai from GH Actions IPs)
with Yahoo Finance quoteSummary (no auth, works from any IP).

Called from morning-login.yml as:
    python -m src.stock_events --cache-event-exclusions
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

from src import notifier, state
from src import stock_config as cfg

_YF_BASE = "https://query1.finance.yahoo.com/v10/finance/quoteSummary"
_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# NSE symbol → Yahoo Finance ticker (only non-trivial mappings)
_YF_SYMBOL_MAP: dict[str, str] = {
    "M&M": "M%26M.NS",
}


def _yf_ticker(name: str) -> str:
    return _YF_SYMBOL_MAP.get(name, f"{name}.NS")


def _ts_to_date(ts: int | None) -> date | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except Exception:
        return None


def _in_window(d: date, today: date) -> bool:
    return today <= d <= today + timedelta(days=cfg.EVENT_LOOKAHEAD_DAYS - 1)


def _has_event(symbol: str, today: date) -> bool:
    """
    Checks Yahoo Finance calendarEvents for upcoming earnings or dividend
    ex-date within EVENT_LOOKAHEAD_DAYS. Returns True if any event falls
    in the window. Raises on HTTP error or unparseable response.
    """
    ticker = _yf_ticker(symbol)
    resp = requests.get(
        f"{_YF_BASE}/{ticker}",
        params={"modules": "calendarEvents"},
        headers=_YF_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    result = (data.get("quoteSummary") or {}).get("result") or []
    if not result:
        return False
    events = result[0].get("calendarEvents") or {}

    # Upcoming earnings dates (Yahoo may return multiple — check all)
    for ts_obj in (events.get("earnings") or {}).get("earningsDate") or []:
        d = _ts_to_date(ts_obj.get("raw"))
        if d and _in_window(d, today):
            print(f"[stock_events] {symbol}: earnings match on {d}")
            return True

    # Dividend ex-date
    ex_div = _ts_to_date((events.get("exDividendDate") or {}).get("raw"))
    if ex_div and _in_window(ex_div, today):
        print(f"[stock_events] {symbol}: ex-dividend match on {ex_div}")
        return True

    return False


def compute_excluded() -> tuple[list[str], int]:
    """
    Returns (excluded_symbols, failure_count) — one pass over cfg.STOCKS.
    Small inter-request delay to stay within Yahoo Finance's rate limits.
    """
    today    = date.today()
    excluded = []
    failures = 0

    for stock in cfg.STOCKS:
        symbol = stock["name"]
        try:
            if _has_event(symbol, today):
                excluded.append(symbol)
                print(f"[stock_events] {symbol}: EXCLUDED")
            else:
                print(f"[stock_events] {symbol}: no event in window")
        except Exception as e:
            print(f"[stock_events] {symbol}: ERROR — {e}")
            failures += 1
        time.sleep(0.5)  # 12 stocks × 0.5s = 6s total; well within YF limits

    return excluded, failures


def cache_event_exclusions() -> None:
    """
    Entry point. Writes the Redis key and sends a tiered Discord warning on
    partial or total failure. Always exits cleanly — never raises.
    """
    try:
        excluded, failures = compute_excluded()
        total = len(cfg.STOCKS)

        today_str = date.today().isoformat()
        key       = f"{cfg.REDIS_EVENT_EXCLUDED_PREFIX}:{today_str}"
        state.redis_set(key, json.dumps(excluded), ex=64800)
        print(f"[stock_events] Redis key written: {key} = {excluded or 'none'}")

        if failures == total:
            notifier.send_warning(
                f"⚠️ STOCK EVENT CHECK: scrape failed for all {total} symbols. "
                "No event exclusions applied today — verify upcoming results/dividends manually."
            )
        elif failures > 0:
            notifier.send_warning(
                f"⚠️ STOCK EVENT CHECK: scrape failed for {failures}/{total} symbols. "
                f"Exclusion list may be incomplete. Excluded so far: {excluded or 'none'}"
            )

    except Exception as e:
        print(f"[stock_events] Unexpected error in cache_event_exclusions: {e}")
        try:
            today_str = date.today().isoformat()
            key       = f"{cfg.REDIS_EVENT_EXCLUDED_PREFIX}:{today_str}"
            state.redis_set(key, json.dumps([]), ex=64800)
            notifier.send_warning(
                f"⚠️ STOCK EVENT CHECK: Unexpected failure ({e}). "
                "Empty exclusion list written — no stocks excluded today."
            )
        except Exception:
            pass


if __name__ == "__main__":
    if "--cache-event-exclusions" in sys.argv:
        cache_event_exclusions()
