"""
stock_events.py — Marketaux-based corporate event exclusion for the stock bot.

Checks Marketaux's news/all API for recent event-language coverage of tracked
stocks (board meeting, AGM, demerger, buyback, bonus/rights issue, M&A, stock
split, IPO, delisting) and writes the set of matching symbols to Redis once a
day from morning-login. Fails open: any failure results in an empty (or
partial) exclusion list plus a Discord report, never a full block of the
stock bot.

Replaced Yahoo Finance quoteSummary (crumb handshake started returning 406
Not Acceptable as of 2026-06-20 — Yahoo's anti-automation check, not a
transient rate limit) which itself had replaced the original NSE
top-corp-info scrape (blocked by Akamai from GH Actions IPs).

KNOWN LIMITATION: unlike Yahoo's calendarEvents (true forward-looking dates),
Marketaux's search is backward-looking — it surfaces recent news mentioning
event language, not a structured future date. This is a real precision
trade-off, accepted because the prior two data sources are unusable (NSE
blocked, Yahoo blocked). See stock-event-exclusion-spec-v3.md §2.

Discord reporting goes to the existing #signals-stocks channel
(DISCORD_STOCK_WEBHOOK_URL) — same channel as live trading alerts, by
deliberate choice (see spec §3), not a separate "stock updates" channel.

Called from morning-login.yml as:
    python -m src.stock_events --cache-event-exclusions
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta

import requests

from src import state
from src import stock_config as cfg

MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"

# All 11 tracked stocks map cleanly to NSE.<SYMBOL> in Marketaux — confirmed by
# live entity-search lookup on 2026-06-20. M&M is NOT in this map: confirmed
# absent from Marketaux's entity database entirely (direct symbol lookup and
# name search both came back empty), and it's also no longer in cfg.STOCKS.
MARKETAUX_SYMBOL_MAP = {s["name"]: f"{s['equity_symbol']}.NS" for s in cfg.STOCKS}

# Free-plan limit is hard-capped at 3 articles/request regardless of requested
# value — confirmed live. Batch into groups of ~4 so one noisy stock in a
# batch doesn't crowd out the others' results within that batch's 3 slots.
_NAMES = list(MARKETAUX_SYMBOL_MAP.keys())
BATCHES = [_NAMES[i:i + 4] for i in range(0, len(_NAMES), 4)]

# Confirmed via live test on 2026-06-20: this exact query, with
# min_match_score=15 and a recency bound, correctly surfaced RIL's real 2026
# AGM and Jio IPO filing news, and correctly suppressed multi-year-old stale
# matches once the date bound was added (omitting it returned a 2021 article).
EVENT_SEARCH_QUERY = (
    '("board meeting"|agm|demerger|buyback|"bonus issue"|"rights issue"|'
    'acquisition|merger|"stock split"|ipo|delisting)'
)
MIN_MATCH_SCORE = 15


def compute_excluded() -> tuple[list[str], int]:
    """
    Returns (excluded_symbols, failed_batch_count) — one pass over BATCHES.
    Each batch wrapped in its own try/except so one bad batch never kills
    the others.
    """
    api_token = os.environ.get("MARKETAUX_API_TOKEN")
    if not api_token:
        print("[stock_events] MARKETAUX_API_TOKEN missing — fail-open, no exclusions")
        return [], len(BATCHES)

    published_after = (date.today() - timedelta(days=cfg.EVENT_LOOKAHEAD_DAYS)).isoformat()
    excluded: list[str] = []
    failures = 0

    for batch_names in BATCHES:
        batch_symbols = [MARKETAUX_SYMBOL_MAP[n] for n in batch_names]
        try:
            resp = requests.get(
                MARKETAUX_URL,
                params={
                    "api_token": api_token,
                    "symbols": ",".join(batch_symbols),
                    "countries": "in",
                    "filter_entities": "true",
                    "must_have_entities": "true",
                    "min_match_score": MIN_MATCH_SCORE,
                    "published_after": published_after,
                    "sort": "published_at",
                    "search": EVENT_SEARCH_QUERY,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[stock_events] Marketaux batch {batch_names} failed: {e}")
            failures += 1
            continue

        if "error" in data:
            print(f"[stock_events] Marketaux error for batch {batch_names}: {data['error']}")
            failures += 1
            continue

        symbol_to_name = {v: k for k, v in MARKETAUX_SYMBOL_MAP.items() if k in batch_names}
        for article in data.get("data", []):
            title = article.get("title", "(no title)")
            for entity in article.get("entities", []):
                symbol = entity.get("symbol")
                name = symbol_to_name.get(symbol)
                if name and name not in excluded:
                    excluded.append(name)
                    print(f"[stock_events] {name}: EXCLUDED — {title}")

    for name in _NAMES:
        if name not in excluded:
            print(f"[stock_events] {name}: no event in window")

    return excluded, failures


def cache_event_exclusions() -> None:
    """
    Entry point. Writes the Redis key (unchanged format/contract) and posts a
    summary to #signals-stocks. Always exits cleanly.
    """
    try:
        excluded, failures = compute_excluded()
        total_batches = len(BATCHES)

        today_str = date.today().isoformat()
        key       = f"{cfg.REDIS_EVENT_EXCLUDED_PREFIX}:{today_str}"
        state.redis_set(key, json.dumps(excluded), ex=64800)
        print(f"[stock_events] Redis key written: {key} = {excluded or 'none'}")

        _post_discord_summary(excluded, failures, total_batches)

    except Exception as e:
        print(f"[stock_events] Unexpected error in cache_event_exclusions: {e}")
        try:
            today_str = date.today().isoformat()
            key       = f"{cfg.REDIS_EVENT_EXCLUDED_PREFIX}:{today_str}"
            state.redis_set(key, json.dumps([]), ex=64800)
            _post_discord_summary([], len(BATCHES), len(BATCHES), hard_error=str(e))
        except Exception:
            pass


def _post_discord_summary(
    excluded: list[str], failures: int, total_batches: int, hard_error: str | None = None
) -> None:
    webhook_url = os.environ.get("DISCORD_STOCK_WEBHOOK_URL")
    if not webhook_url:
        print("[stock_events] DISCORD_STOCK_WEBHOOK_URL not set — skipping Discord post")
        return

    if hard_error:
        title = "⚠️ Stock Event News — unexpected failure"
        color = 0xf87171
        description = f"Unexpected error: {hard_error}. No exclusions applied today."
    elif failures == total_batches:
        title = "⚠️ Stock Event News — all batches failed"
        color = 0xf87171
        description = "Marketaux fetch failed for all batches. No exclusions applied today."
    elif failures > 0:
        title = "⚠️ Stock Event News — partially successful"
        color = 0xf59e0b
        lines = "\n".join(f"• **{n}**" for n in excluded) if excluded else "None"
        description = (
            f"{failures}/{total_batches} batches failed. Exclusion list may be "
            f"incomplete.\n\nExcluded today:\n{lines}"
        )
    else:
        title = "✅ Stock Event News — check complete"
        color = 0x00e5a0 if not excluded else 0xf59e0b
        if excluded:
            lines = "\n".join(f"• **{n}**" for n in excluded)
            description = f"{len(excluded)} stock(s) excluded today:\n{lines}"
        else:
            description = "No corporate events detected. All 11 tracked stocks active today."

    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": "index-fno-signal-bot · stock_events (marketaux)"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        print(f"[stock_events] Discord post failed: {e}")


if __name__ == "__main__":
    if "--cache-event-exclusions" in sys.argv:
        cache_event_exclusions()
