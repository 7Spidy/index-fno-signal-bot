"""
stock_events.py — NSE corporate-event exclusion list for the stock bot.

Scrapes NSE's top-corp-info endpoint for the 7 tracked stocks once a day
(morning-login), and writes the set of symbols with an earnings / dividend /
split / bonus / AGM event in the next EVENT_LOOKAHEAD_DAYS calendar days to
Redis. Fails open: any scrape failure results in an empty (or partial)
exclusion list plus a Discord warning, never a full block of the stock bot.

Called from morning-login.yml as:
    python -m src.stock_events --cache-event-exclusions
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta

import requests

from src import notifier, state
from src import stock_config as cfg

_BASE = "https://www.nseindia.com"

_HEADERS = {
    "Authority": "www.nseindia.com",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nseindia.com",
    "Referer": "https://www.nseindia.com/",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _session() -> requests.Session:
    """Cookie handshake against the homepage, returns a ready session."""
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    sess.get(_BASE, timeout=10)
    return sess


def _in_window(d: date, today: date) -> bool:
    return today <= d <= today + timedelta(days=cfg.EVENT_LOOKAHEAD_DAYS - 1)


def _parse_nse_date(raw: str) -> date | None:
    """Defensive parse — see spec §2. Returns None (and logs) on failure."""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()
    except ValueError:
        print(f"[stock_events] unparseable date: {raw!r}")
        return None


def _has_event(session: requests.Session, symbol: str, today: date) -> bool:
    """
    One GET to /api/top-corp-info for `symbol`. Uses params={...} dict so
    M&M's "&" is percent-encoded correctly — do NOT use an f-string URL here.
    Retries once on 401/403 with a fresh session. Returns True if either
    corporate_actions or borad_meeting has a matching row in the window.
    Raises on any other error — caller's per-symbol try/except handles it.
    """
    def _fetch(sess: requests.Session) -> requests.Response:
        return sess.get(
            f"{_BASE}/api/top-corp-info",
            params={"symbol": symbol, "market": "equities"},
            timeout=10,
        )

    resp = _fetch(session)
    if resp.status_code in (401, 403):
        print(f"[stock_events] {symbol}: {resp.status_code} — rebuilding session and retrying")
        new_sess = _session()
        session.cookies.update(new_sess.cookies)
        resp = _fetch(session)

    resp.raise_for_status()
    data = resp.json()

    # Corporate actions: dividend, bonus, split
    for row in data.get("corporate_actions", {}).get("data", []):
        purpose = (row.get("purpose") or "").lower()
        if not any(kw in purpose for kw in ("dividend", "bonus", "split")):
            continue
        d = _parse_nse_date(row.get("exdate", ""))
        if d and _in_window(d, today):
            print(f"[stock_events] {symbol}: corp-action match — {row.get('purpose')!r} on {d}")
            return True

    # Board meetings (note: NSE typo "borad_meeting" is intentional)
    for row in data.get("borad_meeting", {}).get("data", []):
        d = _parse_nse_date(row.get("meetingdate", ""))
        if d and _in_window(d, today):
            print(f"[stock_events] {symbol}: board-meeting match — {row.get('purpose')!r} on {d}")
            return True

    return False


def compute_excluded() -> tuple[list[str], int]:
    """
    Returns (excluded_symbols, failure_count) — one pass over cfg.STOCKS.
    One shared session is reused across all calls; per-symbol failures are
    caught and counted without killing the other stocks.
    """
    today    = date.today()
    excluded = []
    failures = 0
    session  = _session()

    for stock in cfg.STOCKS:
        symbol = stock["name"]
        try:
            if _has_event(session, symbol, today):
                excluded.append(symbol)
                print(f"[stock_events] {symbol}: EXCLUDED")
            else:
                print(f"[stock_events] {symbol}: no event in window")
        except Exception as e:
            print(f"[stock_events] {symbol}: ERROR — {e}")
            failures += 1

    return excluded, failures


def cache_event_exclusions() -> None:
    """
    Entry point. Writes the Redis key (spec §6) and sends the tiered Discord
    warning (spec §5). Always exits 0 — never raises past this function.
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
                f"⚠️ STOCK EVENT CHECK: NSE scrape failed for all {total} symbols. "
                "No event exclusions applied today — verify upcoming results/dividends manually."
            )
        elif failures > 0:
            notifier.send_warning(
                f"⚠️ STOCK EVENT CHECK: NSE scrape failed for {failures}/{total} symbols. "
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
