"""
worst_faller_entry.py — 15:15 Worst-Faller PE entry orchestration.

Called as the second step of dynamic-universe.yml (same job, same
15:15-IST-triggered run as dynamic_stock_universe.py's existing gainer/loser
computation). Fully independent of that gainer/loser path: only the
loser/PE side gets this new 3-window frequency-vote logic (spec decision 1).

Direct-buy alert = a Discord message telling the human to buy manually, plus
a paper-simulated Redis position tracked for SL/target purposes (spec
decision 2). No real order is ever placed.

CLI:
    python -m src.worst_faller_entry --compute-and-alert
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src import state
from src import stock_config as cfg
from src import worst_faller_notifier, worst_faller_universe
from src.kite_client import fetch_ohlcv, get_kite
from src.stock_kite_client import compute_daily_atr_for_token

IST = ZoneInfo("Asia/Kolkata")

REDIS_POSITION_KEY = "worst_faller:position"   # no TTL — persists until the tracker closes it

ENTRY_HHMM = (15, 15)


def _candle_at_or_before(df, ts: datetime):
    """Last row of df whose timestamp <= ts, or None."""
    sub = df[df["timestamp"] <= ts]
    if sub.empty:
        return None
    return sub.iloc[-1]


def _prior_candle(df, ts: datetime):
    """The candle immediately before the at-or-before(ts) candle, or None."""
    sub = df[df["timestamp"] <= ts]
    if len(sub) < 2:
        return None
    return sub.iloc[-2]


def _resolve_pe_tradingsymbol(name: str, expiry, strike: float, instruments_nfo: list[dict]):
    for i in instruments_nfo:
        if (i.get("name") == name and i.get("expiry") == expiry
                and i.get("instrument_type") == "PE"
                and abs(i.get("strike", -1) - strike) < 1e-6):
            return i["tradingsymbol"], i["instrument_token"]
    return None, None


def compute_and_alert(kite=None) -> None:
    if state.redis_exists(REDIS_POSITION_KEY):
        print("[worst_faller_entry] position already open, skipping new entry")
        return

    kite = kite or get_kite()

    pick = worst_faller_universe.pick_worst_faller(kite)
    if pick is None:
        reason = "no valid worst-faller pick today (insufficient window data or unresolvable universe)"
        print(f"[worst_faller_entry] {reason}")
        worst_faller_notifier.send_skip(reason)
        return

    name = pick["name"]
    strike_step = pick["strike_step"]
    ltp_1515 = pick["ltp_1515"]
    expiry = pick["expiry"]

    atm_strike = round(ltp_1515 / strike_step) * strike_step

    try:
        instruments_nfo = kite.instruments("NFO")
    except Exception as e:
        reason = f"NFO instrument dump fetch failed: {e}"
        print(f"[worst_faller_entry] {reason}")
        worst_faller_notifier.send_skip(reason)
        return

    pe_symbol, pe_token = _resolve_pe_tradingsymbol(name, expiry, atm_strike, instruments_nfo)
    if not pe_symbol:
        reason = f"{name} ATM PE {atm_strike} not found in NFO dump for expiry {expiry}"
        print(f"[worst_faller_entry] {reason}")
        worst_faller_notifier.send_skip(reason)
        return

    today_open_dt = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    target_ts = datetime.now(IST).replace(hour=ENTRY_HHMM[0], minute=ENTRY_HHMM[1], second=0, microsecond=0)

    try:
        opt_df = fetch_ohlcv(pe_token, today_open_dt)
    except Exception as e:
        reason = f"{name} PE {pe_symbol} option candle fetch failed: {e}"
        print(f"[worst_faller_entry] {reason}")
        worst_faller_notifier.send_skip(reason)
        return

    entry_opt_row = _candle_at_or_before(opt_df, target_ts)
    if entry_opt_row is None:
        reason = f"{name} PE {pe_symbol} has no entry-time candle"
        print(f"[worst_faller_entry] {reason}")
        worst_faller_notifier.send_skip(reason)
        return
    entry_opt_price = float(entry_opt_row["close"])

    try:
        spot_df = fetch_ohlcv(pick["equity_token"], today_open_dt)
    except Exception as e:
        reason = f"{name} spot candle fetch failed: {e}"
        print(f"[worst_faller_entry] {reason}")
        worst_faller_notifier.send_skip(reason)
        return

    prior_candle = _prior_candle(spot_df, target_ts)
    if prior_candle is None:
        reason = f"{name} insufficient spot candles before entry"
        print(f"[worst_faller_entry] {reason}")
        worst_faller_notifier.send_skip(reason)
        return
    sl_spot = float(prior_candle["high"])
    risk_pts = abs(ltp_1515 - sl_spot)

    # ── ATR-anchored target, verbatim formula from src/stock_main.py ──
    stock_atr = compute_daily_atr_for_token(kite, pick["equity_token"])
    option_cache_range = round(0.05 * ltp_1515, 1)   # dynamic_stock_universe.py convention
    if stock_atr:
        raw_target_pts = cfg.ATR_TARGET_K * stock_atr
        floor_pts = 0.5 * max(0.0015 * ltp_1515, 2 * cfg.SLIPPAGE_PTS_EST)
        ceiling_pts = 0.8 * option_cache_range
        target_pts = max(floor_pts, min(raw_target_pts, ceiling_pts))
        target_source = "atr"
    else:
        target_pts = risk_pts * 0.75
        target_source = "fallback_1.5R"

    position = {
        "name": name,
        "pe_symbol": pe_symbol,
        "pe_token": pe_token,
        "equity_token": pick["equity_token"],
        "strike": atm_strike,
        "expiry": expiry.isoformat(),
        "lot_size": pick["lot_size"],
        "entry_time": datetime.now(IST).isoformat(),
        "entry_spot": round(ltp_1515, 2),
        "entry_opt_price": round(entry_opt_price, 2),
        "initial_sl_spot": round(sl_spot, 2),
        "target_pts": round(target_pts, 2),
        "target_source": target_source,
        "frequency_count": pick["frequency_count"],
        "tie_break_used": pick["tie_break_used"],
    }

    state.redis_set(REDIS_POSITION_KEY, json.dumps(position))
    worst_faller_notifier.send_entry(position)
    print(f"[worst_faller_entry] ENTRY: {name} PE {pe_symbol} entry_opt={entry_opt_price:.2f} "
          f"sl_spot={sl_spot:.2f} target_pts={target_pts:.2f} ({target_source})")


def main() -> None:
    if "--compute-and-alert" in sys.argv:
        compute_and_alert()


if __name__ == "__main__":
    main()
