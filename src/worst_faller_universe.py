"""
worst_faller_universe.py — live 3-window worst-faller frequency-vote ranking.

Runs once per trading day at 15:15 IST (via dynamic-universe.yml's second
step). Ported from the chat backtest (experiment_315pm_worst_faller_pe.py)
to operate on LIVE Kite data at call time, not historical replay.

Universe/expiry/strike/lot-size resolution is reused by import from
src/dynamic_stock_universe.py — never reimplemented here. This module only
adds the 3-window (today / today+1prior / today+2prior) % -fall ranking and
frequency-vote winner selection.

CLI: none — imported by src/worst_faller_entry.py.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.dynamic_stock_universe import (
    _fetch_equity_tokens,
    _fetch_universe_and_expiry_map,
    _resolve_strike_step_and_lot_size,
)
from src.kite_client import _throttle_historical_call, get_kite

IST = ZoneInfo("Asia/Kolkata")

TOP_N_PER_WINDOW = 3
ENTRY_HHMM = (15, 15)


def _fetch_daily_ohlc(kite, token: int, days_back: int = 10) -> list[dict]:
    """Last `days_back` calendar days of daily candles, ascending by date.
    Today's candle (if the market is open) comes back in-progress/live,
    per Kite's documented behaviour for interval="day"."""
    today = datetime.now(IST).date()
    from_date = datetime.combine(today - timedelta(days=days_back), datetime.min.time())
    to_date = datetime.combine(today, datetime.min.time()) + timedelta(days=1)
    _throttle_historical_call()
    try:
        return kite.historical_data(
            instrument_token=token, from_date=from_date, to_date=to_date,
            interval="day", continuous=False, oi=False,
        )
    except Exception as e:
        print(f"[worst_faller_universe] daily OHLC fetch failed for token {token}: {e}")
        return []


def _fetch_today_5m(kite, token: int) -> list[dict]:
    """Today's 5-minute candles only."""
    today = datetime.now(IST).date()
    from_date = datetime.combine(today, datetime.min.time())
    to_date = datetime.now(IST)
    _throttle_historical_call()
    try:
        return kite.historical_data(
            instrument_token=token, from_date=from_date, to_date=to_date,
            interval="5minute", continuous=False, oi=False,
        )
    except Exception as e:
        print(f"[worst_faller_universe] 5-min fetch failed for token {token}: {e}")
        return []


def _candle_at_or_before(candles: list[dict], target: datetime):
    """Last candle whose date <= target, or None."""
    eligible = [c for c in candles if c["date"].replace(tzinfo=None) <= target.replace(tzinfo=None)]
    return eligible[-1] if eligible else None


def _resolve_windows(kite, name: str, token: int) -> dict | None:
    """Returns {"today_open": f, "ltp_1515": f, "w1_pct": f, "w2_pct": f|None,
    "w3_pct": f|None} or None if today's open / 15:15 LTP can't be resolved."""
    daily = _fetch_daily_ohlc(kite, token)
    if len(daily) < 1:
        return None

    today = datetime.now(IST).date()

    def _row_date(row):
        d = row["date"]
        return d.date() if hasattr(d, "date") else d

    daily_by_date = {_row_date(r): r for r in daily}
    if today not in daily_by_date:
        return None
    today_open = float(daily_by_date[today]["open"])
    if today_open <= 0:
        return None

    distinct_dates = sorted(d for d in daily_by_date if d < today)
    prior_1 = distinct_dates[-1] if len(distinct_dates) >= 1 else None
    prior_2 = distinct_dates[-2] if len(distinct_dates) >= 2 else None

    intraday = _fetch_today_5m(kite, token)
    if not intraday:
        return None
    target_ts = datetime.now(IST).replace(
        hour=ENTRY_HHMM[0], minute=ENTRY_HHMM[1], second=0, microsecond=0,
    )
    ltp_row = _candle_at_or_before(intraday, target_ts)
    if ltp_row is None:
        return None
    ltp_1515 = float(ltp_row["close"])

    w1_pct = (ltp_1515 - today_open) / today_open * 100

    w2_pct = None
    if prior_1 is not None:
        base = float(daily_by_date[prior_1]["open"])
        if base > 0:
            w2_pct = (ltp_1515 - base) / base * 100

    w3_pct = None
    if prior_2 is not None:
        base = float(daily_by_date[prior_2]["open"])
        if base > 0:
            w3_pct = (ltp_1515 - base) / base * 100

    return {
        "today_open": today_open,
        "ltp_1515": ltp_1515,
        "w1_pct": w1_pct,
        "w2_pct": w2_pct,
        "w3_pct": w3_pct,
    }


def pick_worst_faller(kite=None) -> dict | None:
    """Runs the live 3-window worst-faller frequency vote once and returns a
    fully-resolved pick dict, or None if fewer than TOP_N_PER_WINDOW valid
    fallers exist in any window (early/holiday-shortened data etc)."""
    kite = kite or get_kite()

    try:
        instruments_nfo = kite.instruments("NFO")
        instruments_nse = kite.instruments("NSE")
    except Exception as e:
        print(f"[worst_faller_universe] instrument dump fetch failed: {e}")
        return None

    expiry_map = _fetch_universe_and_expiry_map(instruments_nfo)
    if not expiry_map:
        print("[worst_faller_universe] empty NFO universe after filtering")
        return None

    equity_tokens = _fetch_equity_tokens(instruments_nse, set(expiry_map.keys()))
    if not equity_tokens:
        print("[worst_faller_universe] no resolvable equity tokens")
        return None

    w1_falls: list[tuple[str, float]] = []
    w2_falls: list[tuple[str, float]] = []
    w3_falls: list[tuple[str, float]] = []
    today_open_map: dict[str, float] = {}
    ltp_1515_map: dict[str, float] = {}

    for name, token in equity_tokens.items():
        windows = _resolve_windows(kite, name, token)
        if windows is None:
            continue
        today_open_map[name] = windows["today_open"]
        ltp_1515_map[name] = windows["ltp_1515"]
        w1_falls.append((name, windows["w1_pct"]))
        if windows["w2_pct"] is not None:
            w2_falls.append((name, windows["w2_pct"]))
        if windows["w3_pct"] is not None:
            w3_falls.append((name, windows["w3_pct"]))

    if (len(w1_falls) < TOP_N_PER_WINDOW
            or len(w2_falls) < TOP_N_PER_WINDOW
            or len(w3_falls) < TOP_N_PER_WINDOW):
        print(f"[worst_faller_universe] insufficient fallers in one of the windows "
              f"(w1={len(w1_falls)}, w2={len(w2_falls)}, w3={len(w3_falls)}) — no pick today")
        return None

    top_w1 = sorted(w1_falls, key=lambda x: x[1])[:TOP_N_PER_WINDOW]
    top_w2 = sorted(w2_falls, key=lambda x: x[1])[:TOP_N_PER_WINDOW]
    top_w3 = sorted(w3_falls, key=lambda x: x[1])[:TOP_N_PER_WINDOW]

    all_slots = [n for n, _ in top_w1] + [n for n, _ in top_w2] + [n for n, _ in top_w3]
    counts = Counter(all_slots)
    max_count = max(counts.values())
    candidates = [n for n, c in counts.items() if c == max_count]

    tie_break_used = False
    if len(candidates) == 1:
        winner = candidates[0]
    else:
        tie_break_used = True
        w1_dict = dict(w1_falls)
        winner = min(candidates, key=lambda n: w1_dict.get(n, 0.0))

    expiries = expiry_map.get(winner)
    if not expiries:
        print(f"[worst_faller_universe] {winner} has no resolvable expiry — no pick today")
        return None

    resolved = _resolve_strike_step_and_lot_size(winner, expiries[0], instruments_nfo)
    if not resolved:
        print(f"[worst_faller_universe] {winner} strike/lot resolution failed — no pick today")
        return None
    strike_step, lot_size = resolved

    w1_dict = dict(w1_falls)
    w2_dict = dict(w2_falls)
    w3_dict = dict(w3_falls)

    return {
        "name": winner,
        "equity_token": equity_tokens[winner],
        "expiry": expiries[0],
        "strike_step": strike_step,
        "lot_size": lot_size,
        "today_open": today_open_map[winner],
        "ltp_1515": ltp_1515_map[winner],
        "pct_falls": {
            "w1": round(w1_dict.get(winner, 0.0), 2),
            "w2": round(w2_dict.get(winner, 0.0), 2),
            "w3": round(w3_dict.get(winner, 0.0), 2),
        },
        "frequency_count": max_count,
        "tie_break_used": tie_break_used,
    }
