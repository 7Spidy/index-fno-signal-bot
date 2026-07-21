"""
dynamic_stock_universe.py — Daily top-gainer/top-loser F&O stock discovery.

Computes, once per trading day (EOD ~15:30 IST via dynamic-universe.yml), the
single best 3-session close-to-close top gainer and top loser from the live
NFO monthly-equity-derivative universe, and caches a fully-resolved pick for
each to Redis (stock:dynamic_universe). stock_main.py, position_tracker.py,
and paper_engine.py read this at runtime and merge it into the static
14-stock pipeline for the day.

Top gainer -> CE only. Top loser -> PE only. Both follow the exact same
C1-C5 chain as the static 14 (stock_main.py._evaluate) -- no new signal
logic, no special-cased thresholds. Alert + paper-track only -- never
written to the live executor intent path (see spec Section 0.3).

Fails open: any failure (Kite API issue, no valid candidate within
MAX_CANDIDATE_TRIES) results in fewer or zero dynamic picks for the day,
never a blocked static-14 run.

Called from dynamic-universe.yml as:
    python -m src.dynamic_stock_universe --compute-and-cache
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta

from src import calendar_nse, notifier, state
from src import stock_config as cfg
from src.calendar_nse import IST
from src.kite_client import _throttle_historical_call, get_kite
from src.stock_kite_client import compute_daily_atr_for_token

_INDEX_NAMES_EXCLUDE = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def _fetch_universe_and_expiry_map(instruments: list[dict]) -> dict[str, list[date]]:
    """name -> sorted list of distinct future monthly expiries, NFO options only,
    indices excluded. Built once from an already-fetched kite.instruments('NFO')
    dump -- no extra API call."""
    today = datetime.now(IST).date()
    universe: dict[str, set[date]] = {}
    for i in instruments:
        name = i.get("name")
        if not name or name in _INDEX_NAMES_EXCLUDE:
            continue
        if i.get("instrument_type") not in ("CE", "PE"):
            continue
        expiry = i.get("expiry")
        if not expiry or expiry < today:
            continue
        universe.setdefault(name, set()).add(expiry)
    return {name: sorted(exps) for name, exps in universe.items()}


def _fetch_equity_tokens(instruments_nse: list[dict], names: set[str]) -> dict[str, int]:
    """{tradingsymbol: instrument_token} for every name in `names` that has an
    NSE EQ listing. Built from one kite.instruments('NSE') dump."""
    result = {}
    for inst in instruments_nse:
        sym = inst.get("tradingsymbol", "")
        if sym in names and inst.get("instrument_type") == "EQ":
            result[sym] = inst["instrument_token"]
    return result


def _resolve_strike_step_and_lot_size(
    name: str, expiry: date, instruments_nfo: list[dict]
) -> tuple[float, int] | None:
    """Derive strike_step (min gap between distinct listed strikes) and
    lot_size from the already-fetched NFO dump for this name+expiry. Returns
    None if fewer than 2 distinct strikes are listed (can't infer a step)."""
    strikes = sorted({
        i["strike"] for i in instruments_nfo
        if i.get("name") == name and i.get("expiry") == expiry
        and i.get("instrument_type") in ("CE", "PE") and i.get("strike")
    })
    if len(strikes) < 2:
        return None
    gaps = [round(b - a, 4) for a, b in zip(strikes, strikes[1:]) if b > a]
    if not gaps:
        return None
    step = min(gaps)
    lot_size = next(
        (i["lot_size"] for i in instruments_nfo
         if i.get("name") == name and i.get("expiry") == expiry
         and i.get("instrument_type") in ("CE", "PE")),
        None,
    )
    if not lot_size:
        return None
    return step, lot_size


def _compute_3session_change(kite, token: int) -> tuple[float, float] | None:
    """Returns (pct_change_3session, latest_close) or None on failure /
    insufficient data. Needs >= 4 daily closes (today's + 3 sessions back)."""
    today = datetime.now(IST).date()
    from_date = datetime.combine(today - timedelta(days=10), datetime.min.time())
    to_date = datetime.combine(today, datetime.min.time())
    _throttle_historical_call()
    try:
        candles = kite.historical_data(
            instrument_token=token, from_date=from_date, to_date=to_date,
            interval="day", continuous=False, oi=False,
        )
    except Exception as e:
        print(f"[dynamic_universe] historical fetch failed for token {token}: {e}")
        return None
    if len(candles) < cfg.DYNAMIC_UNIVERSE_LOOKBACK_SESSIONS + 1:
        return None
    recent = candles[-(cfg.DYNAMIC_UNIVERSE_LOOKBACK_SESSIONS + 1):]
    base_close, latest_close = recent[0]["close"], recent[-1]["close"]
    if not base_close:
        return None
    pct = (latest_close - base_close) / base_close * 100
    return round(pct, 2), latest_close


def _pick_candidate(
    ranked: list[tuple[str, float, float]],  # (name, pct_change, latest_close), best-first
    exclude_names: set[str],
    expiry_map: dict[str, list[date]],
    equity_tokens: dict[str, int],
    instruments_nfo: list[dict],
    max_tries: int,
) -> dict | None:
    """Walk the ranked list up to max_tries entries, skipping any name already
    in exclude_names or missing a resolvable expiry/strike-step/lot-size/
    equity-token. Returns a fully-resolved pick dict or None."""
    tries = 0
    for name, pct_change, latest_close in ranked:
        if name in exclude_names:
            continue
        tries += 1
        if tries > max_tries:
            break
        expiries = expiry_map.get(name)
        if not expiries:
            continue
        resolved = _resolve_strike_step_and_lot_size(name, expiries[0], instruments_nfo)
        if not resolved:
            continue
        strike_step, lot_size = resolved
        equity_token = equity_tokens.get(name)
        if not equity_token:
            continue
        return {
            "name": name,
            "equity_symbol": name,
            "sector": None,
            "strike_step": strike_step,
            "lot_size": lot_size,
            "fno_exchange": "NFO",
            "spot_exchange": "NSE",
            "is_dynamic": True,
            "option_cache_range": round(0.05 * latest_close, 1),
            "equity_token": equity_token,
            "pct_change_3session": pct_change,
            "candidates_tried": tries,
        }
    return None


def compute_and_cache_dynamic_universe() -> None:
    if not calendar_nse.is_trading_day():
        print("[dynamic_universe] Not a trading day — exiting")
        return

    kite = get_kite()

    try:
        instruments_nfo = kite.instruments("NFO")
        instruments_nse = kite.instruments("NSE")
    except Exception as e:
        notifier.send_warning(f"⚠️ DYNAMIC UNIVERSE: instrument dump fetch failed: {e}. No picks today.")
        return

    expiry_map = _fetch_universe_and_expiry_map(instruments_nfo)
    if not expiry_map:
        notifier.send_warning("⚠️ DYNAMIC UNIVERSE: empty NFO universe after filtering. No picks today.")
        return

    equity_tokens = _fetch_equity_tokens(instruments_nse, set(expiry_map.keys()))

    ranked: list[tuple[str, float, float]] = []
    for name, token in equity_tokens.items():
        result = _compute_3session_change(kite, token)
        if result is None:
            continue
        pct, latest_close = result
        ranked.append((name, pct, latest_close))

    if not ranked:
        notifier.send_warning("⚠️ DYNAMIC UNIVERSE: no stock had computable 3-session change. No picks today.")
        return

    static_names = set(cfg.STOCK_BY_NAME.keys())
    ranked_gainers = sorted(ranked, key=lambda r: r[1], reverse=True)
    ranked_losers = sorted(ranked, key=lambda r: r[1])

    gainer = _pick_candidate(
        ranked_gainers, static_names, expiry_map, equity_tokens, instruments_nfo,
        cfg.DYNAMIC_UNIVERSE_MAX_CANDIDATE_TRIES,
    )
    exclude_for_loser = set(static_names)
    if gainer:
        exclude_for_loser.add(gainer["name"])
    loser = _pick_candidate(
        ranked_losers, exclude_for_loser, expiry_map, equity_tokens, instruments_nfo,
        cfg.DYNAMIC_UNIVERSE_MAX_CANDIDATE_TRIES,
    )

    picks = []
    if gainer:
        gainer["direction_restriction"] = "CE_ONLY"
        gainer["rank_basis"] = "top_gainer_3session"
        atr = compute_daily_atr_for_token(kite, gainer["equity_token"])
        if atr:
            gainer["atr"] = atr
        picks.append(gainer)
    if loser:
        loser["direction_restriction"] = "PE_ONLY"
        loser["rank_basis"] = "top_loser_3session"
        atr = compute_daily_atr_for_token(kite, loser["equity_token"])
        if atr:
            loser["atr"] = atr
        picks.append(loser)

    payload = {
        "date": calendar_nse.next_trading_day().isoformat(),
        "picks": picks,
        "gainer_found": gainer is not None,
        "loser_found": loser is not None,
    }
    state.redis_set(cfg.DYNAMIC_UNIVERSE_REDIS_KEY, json.dumps(payload), ex=93600)

    _post_summary(gainer, loser)


def _post_summary(gainer: dict | None, loser: dict | None) -> None:
    """Posts the routine daily pick summary to #signals-stocks directly via
    DISCORD_STOCK_WEBHOOK_URL — NOT via notifier.send_warning(), which reads
    DISCORD_WEBHOOK_URL (the main channel) and would silently no-op here
    since this job's env only sets the stock-specific webhook."""
    import os
    import requests
    from datetime import datetime, timezone

    webhook_url = os.environ.get("DISCORD_STOCK_WEBHOOK_URL")
    if not webhook_url:
        print("[dynamic_universe] DISCORD_STOCK_WEBHOOK_URL not set — skipping Discord post")
        return

    lines = []
    if gainer:
        lines.append(f"📈 Top gainer (CE-only): **{gainer['name']}** "
                      f"({gainer['pct_change_3session']:+.2f}% / 3 sessions)")
    else:
        lines.append("📈 Top gainer: none found within candidate-try budget")
    if loser:
        lines.append(f"📉 Top loser (PE-only): **{loser['name']}** "
                      f"({loser['pct_change_3session']:+.2f}% / 3 sessions)")
    else:
        lines.append("📉 Top loser: none found within candidate-try budget")

    title = "✅ Dynamic Stock Universe — computed" if (gainer or loser) \
        else "⚠️ Dynamic Stock Universe — no picks today"
    embed = {
        "title": title,
        "description": "\n".join(lines),
        "color": 0x00e5a0 if (gainer or loser) else 0xf59e0b,
        "footer": {"text": "index-fno-signal-bot · dynamic_stock_universe"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"[dynamic_universe] Discord post failed: {e}")


def get_active_dynamic_stocks() -> list[dict]:
    """Reader used by stock_main.py / position_tracker.py / paper_engine.py.
    Returns [] if the key is missing OR stale (not today's date) -- a
    previous day's EOD-job failure must never leak yesterday's picks into
    today's run."""
    raw = state.redis_get(cfg.DYNAMIC_UNIVERSE_REDIS_KEY)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    if payload.get("date") != datetime.now(IST).date().isoformat():
        return []
    return payload.get("picks", [])


if __name__ == "__main__":
    if "--compute-and-cache" in sys.argv:
        compute_and_cache_dynamic_universe()
