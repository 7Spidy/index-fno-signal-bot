"""
worst_faller_tracker.py — per-minute tracker tick for the 15:15 Worst-Faller
PE paper position.

Mirrors src/condor_engine.py's --tracker-tick entry point. Reads the open
position from Redis (worst_faller:position, written by worst_faller_entry.py),
pulls current spot + option LTP, walks the exact trailing-SL ladder
(compute_ladder_sl -> compute_ai_adjusted_sl -> compute_final_sl, imported
verbatim from src/position_tracker.py — never reimplemented), checks for an
SL/target breach, and posts an edit-in-place update or a close embed.

Runs every 1 minute during market hours (workflow_dispatch, triggered by
cron-job.org — see .github/workflows/worst-faller-tracker.yml). Continues
across days automatically if the position carries overnight, since it just
reads Redis state each tick (no special-casing needed), exactly like
condor-tracker.yml.

CLI:
    python -m src.worst_faller_tracker --tracker-tick
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

from src import state, worst_faller_notifier
from src.indicators import rsi_wilder
from src.kite_client import fetch_ohlcv, get_kite
from src.position_tracker import compute_ai_adjusted_sl, compute_final_sl, compute_ladder_sl
from src.worst_faller_entry import REDIS_POSITION_KEY

IST = ZoneInfo("Asia/Kolkata")


def _load_position() -> dict | None:
    raw = state.redis_get(REDIS_POSITION_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _rsi_last3(equity_token: int) -> list[float] | None:
    today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    try:
        df = fetch_ohlcv(equity_token, today_open)
    except Exception as e:
        print(f"[worst_faller_tracker] RSI candle fetch failed: {e}")
        return None
    rsi_series = rsi_wilder(df).dropna()
    if len(rsi_series) < 3:
        return None
    return list(rsi_series.tail(3).values)


def tracker_tick(kite=None) -> None:
    position = _load_position()
    if position is None:
        return   # no open position — cheap no-op, most ticks outside a trade land here

    kite = kite or get_kite()

    spot_key = f"NSE:{position['equity_token']}"
    opt_key = f"NFO:{position['pe_token']}"
    try:
        quotes = kite.quote([spot_key, opt_key])
    except Exception as e:
        print(f"[worst_faller_tracker] quote fetch failed: {e}")
        return

    spot_q = quotes.get(spot_key)
    opt_q = quotes.get(opt_key)
    if not spot_q or not opt_q:
        print("[worst_faller_tracker] missing quote for spot or option — skip cycle")
        return

    current_spot = float(spot_q["last_price"])
    ohlc = spot_q.get("ohlc") or {}
    spot_high = float(ohlc.get("high") or current_spot)
    spot_low = float(ohlc.get("low") or current_spot)
    current_opt_ltp = float(opt_q["last_price"])

    entry_spot = position["entry_spot"]
    target_pts = position["target_pts"]
    prior_sl = position.get("current_sl_spot", position["initial_sl_spot"])

    progress = (entry_spot - current_spot) / target_pts if target_pts else 0.0

    ladder_sl = compute_ladder_sl(entry_spot, target_pts, current_spot, "PE", prior_sl)

    rsi_last3 = _rsi_last3(position["equity_token"])
    ai_sl = compute_ai_adjusted_sl(ladder_sl, "PE", {
        "rsi_last3": rsi_last3,
        "progress": progress,
        "current_price": current_spot,
        "T": target_pts,
    })
    final_sl = compute_final_sl(ladder_sl, ai_sl, "PE")

    position["current_sl_spot"] = round(final_sl, 2)

    exit_reason = None
    if spot_high >= final_sl:
        exit_reason = "SL_HIT"
    elif spot_low <= entry_spot - target_pts:
        exit_reason = "TARGET_HIT"

    pnl_rs = (current_opt_ltp - position["entry_opt_price"]) * position["lot_size"]

    if exit_reason:
        print(f"[worst_faller_tracker] EXIT ({exit_reason}): {position['name']} pnl_rs={pnl_rs:.2f}")
        worst_faller_notifier.send_close(position, current_spot, final_sl, current_opt_ltp, pnl_rs, exit_reason)
        state.redis_delete(REDIS_POSITION_KEY)
        return

    state.redis_set(REDIS_POSITION_KEY, json.dumps(position))
    worst_faller_notifier.send_update(position, current_spot, final_sl, current_opt_ltp, pnl_rs)


def main() -> None:
    if "--tracker-tick" in sys.argv:
        tracker_tick()


if __name__ == "__main__":
    main()
