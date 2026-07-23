"""
Paper-trade position tracker — simulates trades, never places real orders.

Triggered every minute by cron-job.org → workflow_dispatch on trade-tracker.yml.

Flow:
  1. For each of the 17 instruments, read tracker:pending_intent:{INSTRUMENT}.
     If a fresh, unconsumed intent exists, call paper_engine.simulate_entry().
  2. For each open paper position, fetch live option LTP, compute the ladder SL,
     and check whether the SL has been crossed. If crossed, call simulate_exit().
  3. At 15:30 IST, square off any remaining open positions at live LTP (EOD).
  4. Post/edit the single consolidated Discord message each cycle.
  5. After EOD square-off, post the daily summary exactly once.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src import config
from src import paper_engine
from src import state
from src import stock_config
from src import trade_notifier

IST = ZoneInfo("Asia/Kolkata")

# ──────────────────────────────────────────────────────────────
# Sub-minute loop configuration
# ──────────────────────────────────────────────────────────────
# The workflow is dispatched once per minute. Within a single process we run
# up to TRACKER_SUBLOOPS heartbeat passes spaced TRACKER_SUBLOOP_SECS apart,
# but only while at least one paper position is open. Read from env at call
# time (not import time) so tests can monkeypatch without module reload.

_DEFAULT_SUBLOOPS      = 4
_DEFAULT_SUBLOOP_SECS  = 15.0
_DEFAULT_BUDGET_SECS   = 55.0


def _loop_config() -> tuple[int, float, float]:
    """Returns (subloops, interval_secs, budget_secs). Set TRACKER_SUBLOOPS=1
    to fully revert to single-pass behaviour with no code change."""
    def _num(name, default, cast):
        try:
            return cast(os.environ.get(name, "").strip() or default)
        except (TypeError, ValueError):
            return default

    subloops = max(1, _num("TRACKER_SUBLOOPS", _DEFAULT_SUBLOOPS, int))
    interval = max(1.0, _num("TRACKER_SUBLOOP_SECS", _DEFAULT_SUBLOOP_SECS, float))
    budget   = max(1.0, _num("TRACKER_LOOP_BUDGET_SECS", _DEFAULT_BUDGET_SECS, float))
    return subloops, interval, budget


def _elapsed_since_job_start() -> float:
    """Seconds consumed before this Python process started (Actions checkout +
    pip install). The workflow exports TRACKER_JOB_START_EPOCH as its first
    step. Returns 0.0 when absent (local runs), which makes the budget behave
    as if measured from process start."""
    raw = os.environ.get("TRACKER_JOB_START_EPOCH", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, time.time() - float(raw))
    except (TypeError, ValueError):
        return 0.0


# Legacy key — kept so existing tests against _position_key / _load_position pass
INDEX_KEY = "position:index"

# All instruments: 3 indices + 14 static stocks + today's dynamic picks (if any)
def _index_names() -> list[str]:
    return [inst["name"] for inst in config.INSTRUMENTS]


def _dynamic_stock_names() -> list[str]:
    from src import dynamic_stock_universe
    return [p["name"] for p in dynamic_stock_universe.get_active_dynamic_stocks()]


def _stock_names() -> list[str]:
    return list(stock_config.STOCK_BY_NAME.keys()) + _dynamic_stock_names()


def _all_instruments() -> list[str]:
    return _index_names() + _stock_names()


# ──────────────────────────────────────────────────────────────
# Ladder / SL computation — kept exactly as before
# ──────────────────────────────────────────────────────────────

def compute_ladder_sl(
    entry: float,
    T: float,
    current_price: float,
    direction: str,
    prior_sl: float,
) -> float:
    """Monotonic trailing SL via a mechanical progress ladder.

    direction must be "CE" or "PE" (case-insensitive). Anything else raises
    ValueError — this is a programming bug, not bad market data.

    T must be > 0. If T <= 0 or current_price is None, returns prior_sl unchanged
    and logs a warning.

    Ladder (sl_fraction applied when progress reaches each threshold):
      progress >= 0.5      → sl_fraction = 0.25
      progress >= 0.9      → sl_fraction = 0.60
      progress >= 1.0      → sl_fraction = 0.90
      progress >= 1.0+0.1n → sl_fraction = 0.90 + 0.10*n  (n>=1, each +0.1T step)

    sl_price = entry + sl_fraction * T  (CE)
             = entry - sl_fraction * T  (PE)

    Final return is monotonically non-decreasing (CE) / non-increasing (PE)
    relative to prior_sl.
    """
    direction = direction.upper()
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")

    if T is None or T <= 0:
        print(f"[position_tracker] compute_ladder_sl: T={T!r} invalid — returning prior_sl")
        return prior_sl

    if current_price is None:
        print("[position_tracker] compute_ladder_sl: current_price is None — returning prior_sl")
        return prior_sl

    if direction == "CE":
        progress = (current_price - entry) / T
    else:
        progress = (entry - current_price) / T

    if progress < 0.5:
        return prior_sl

    if progress < 0.9:
        sl_fraction = 0.25
    elif progress < 1.0:
        sl_fraction = 0.60
    else:
        n = math.floor(round((progress - 1.0) / 0.1, 9))
        sl_fraction = 0.9 + 0.1 * n

    if direction == "CE":
        sl_price = entry + sl_fraction * T
        return max(sl_price, prior_sl)
    else:
        sl_price = entry - sl_fraction * T
        return min(sl_price, prior_sl)


def _rsi_reversing(direction: str, rsi_values: list) -> bool:
    """Return True if the RSI 3-point staircase is reversing against the trade."""
    r0, r1, r2 = rsi_values[0], rsi_values[1], rsi_values[2]
    if direction.upper() == "CE":
        return r1 < r0 and r2 < r1
    return r1 > r0 and r2 > r1


def compute_ai_adjusted_sl(
    ladder_sl: float,
    direction: str,
    market_snapshot: dict,
) -> float:
    """Rule-based heuristic that may ONLY tighten the SL vs the ladder.

    v1 implementation: deterministic, no LLM call. If RSI shows a 3-point
    reversal against the position's direction AND progress >= 0.7T, tighten SL
    to current_price ∓ 0.05*T.
    """
    direction = direction.upper()
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")

    rsi_values    = market_snapshot.get("rsi_last3")
    progress      = market_snapshot.get("progress", 0.0)
    current_price = market_snapshot.get("current_price")
    T             = market_snapshot.get("T")

    if (
        rsi_values is None
        or len(rsi_values) < 3
        or progress < 0.7
        or current_price is None
        or T is None
        or T <= 0
    ):
        return ladder_sl

    r0, r1, r2 = rsi_values[0], rsi_values[1], rsi_values[2]

    if not _rsi_reversing(direction, [r0, r1, r2]):
        return ladder_sl

    if direction == "CE":
        tightened = current_price - 0.05 * T
        return max(tightened, ladder_sl)
    else:
        tightened = current_price + 0.05 * T
        return min(tightened, ladder_sl)


def compute_final_sl(ladder_sl: float, ai_sl: float, direction: str) -> float:
    """Combine ladder SL and AI-adjusted SL, always taking the tighter side."""
    direction = direction.upper()
    if direction == "CE":
        return max(ladder_sl, ai_sl)
    return min(ladder_sl, ai_sl)


# ──────────────────────────────────────────────────────────────
# Legacy Redis helpers — kept as dead code so existing tests pass.
# These keys (position:{tradingsymbol}, position:index) are NOT used
# by the paper-trade path; the paper path uses paper:position:* keys.
# ──────────────────────────────────────────────────────────────

def _seconds_until_next_1600_ist() -> int:
    """TTL target: next occurrence of 16:00 IST."""
    now    = datetime.now(IST)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(int((target - now).total_seconds()), 60)


def _position_key(tradingsymbol: str) -> str:
    return f"position:{tradingsymbol}"


def _load_position(tradingsymbol: str) -> dict | None:
    raw = state.redis_get(_position_key(tradingsymbol))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _load_index() -> list[str]:
    raw = state.redis_get(INDEX_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_index(symbols: list[str]) -> None:
    state.redis_set(INDEX_KEY, json.dumps(symbols), ex=_seconds_until_next_1600_ist())


def _add_to_index(tradingsymbol: str) -> None:
    idx = _load_index()
    if tradingsymbol not in idx:
        idx.append(tradingsymbol)
        _save_index(idx)


def _remove_from_index(tradingsymbol: str) -> None:
    idx = _load_index()
    if tradingsymbol in idx:
        idx.remove(tradingsymbol)
        _save_index(idx)


def _save_position(tradingsymbol: str, data: dict) -> None:
    state.redis_set(_position_key(tradingsymbol), json.dumps(data), ex=_seconds_until_next_1600_ist())
    _add_to_index(tradingsymbol)


def _delete_position(tradingsymbol: str) -> None:
    state.redis_delete(_position_key(tradingsymbol))
    _remove_from_index(tradingsymbol)


# ──────────────────────────────────────────────────────────────
# Instrument / direction extraction from tradingsymbol
# ──────────────────────────────────────────────────────────────

def _underlying_from_tradingsymbol(tradingsymbol: str) -> str | None:
    """Extract underlying name from a Kite tradingsymbol.

    Examples: NIFTY26JUN24600CE → NIFTY, MARUTI26JUL14300CE → MARUTI
    """
    sym = tradingsymbol.upper()
    known_underlyings = sorted(
        set(_index_names()) | set(_stock_names()), key=len, reverse=True
    )
    for name in known_underlyings:
        if sym.startswith(name):
            return name
    return None


def _asset_class_for(instrument: str) -> str:
    """INDEX for config.INSTRUMENTS names, STOCK for stock_config.STOCKS names
    or today's dynamic picks."""
    if instrument in _index_names():
        return "INDEX"
    if instrument in stock_config.STOCK_BY_NAME:
        return "STOCK"
    if instrument in _dynamic_stock_names():
        return "STOCK"
    return "UNKNOWN"


_CE_PE_SUFFIX_RE = re.compile(r"\d(CE|PE)$")


def _direction_from_tradingsymbol(tradingsymbol: str) -> str | None:
    """Extract CE/PE from a tradingsymbol (suffix must follow a digit)."""
    sym = tradingsymbol.upper()
    m = _CE_PE_SUFFIX_RE.search(sym)
    return m.group(1) if m else None


# ──────────────────────────────────────────────────────────────
# Intent payload lookup
# ──────────────────────────────────────────────────────────────

def _load_tracker_intent(instrument: str) -> dict | None:
    """Load per-instrument tracker-intent from Redis."""
    raw = state.redis_get(f"tracker:pending_intent:{instrument.upper()}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if data.get("instrument", "").upper() != instrument.upper():
        return None
    return data


def _load_intent(instrument: str) -> dict | None:
    """Load most recent Redis intent payload for this instrument.

    Priority:
      1. Legacy global executor keys (executor:pending_intent / executor:position)
      2. Per-instrument tracker key (tracker:pending_intent:{instrument})
    """
    for key in ("executor:pending_intent", "executor:position"):
        raw = state.redis_get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if data.get("instrument", "").upper() == instrument.upper():
            return data

    return _load_tracker_intent(instrument)


# ──────────────────────────────────────────────────────────────
# RSI snapshot for AI-adjusted SL
# ──────────────────────────────────────────────────────────────

# RSI cache: fetch_ohlcv() pulls 5-minute candles, which cannot change more
# than once per 5 minutes — refetching them on every 15s sub-minute pass is
# pure waste against the Kite historical-data quota. The cache is injected
# per-call (see `cache` param) rather than held as module state: a single
# dict is created once in main() and threaded through every pass of one
# process's loop, keyed on the 5-minute bucket so it self-invalidates at
# each candle boundary. When cache is None (e.g. direct/legacy calls),
# caching is skipped entirely and every call fetches fresh.


def _bucket_5m(now: datetime) -> str:
    return now.replace(minute=(now.minute // 5) * 5,
                       second=0, microsecond=0).isoformat()


def _get_rsi_snapshot(
    instrument: str,
    today_open: datetime,
    asset_class: str = "INDEX",
    cache: dict[tuple[str, str], list[float]] | None = None,
) -> list[float] | None:
    """Fetch last 3 RSI values for the instrument.

    INDEX: futures token from kite:instrument_tokens.
    STOCK: equity token from stock_config.REDIS_EQUITY_TOKENS_KEY.
    Returns [r_oldest, r_middle, r_newest] or None on failure.
    """
    cache_key = (instrument, _bucket_5m(datetime.now(IST)))
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        from src import kite_client, indicators, state as st

        if asset_class == "STOCK":
            raw = st.redis_get(stock_config.REDIS_EQUITY_TOKENS_KEY)
            tokens   = json.loads(raw) if raw else {}
            token_id = tokens.get(instrument)
            if not token_id:
                from src import dynamic_stock_universe
                for p in dynamic_stock_universe.get_active_dynamic_stocks():
                    if p["name"] == instrument:
                        token_id = p.get("equity_token")
                        break
            if not token_id:
                return None
        else:
            raw = st.redis_get("kite:instrument_tokens")
            if not raw:
                return None
            tokens     = json.loads(raw)
            token_info = tokens.get(instrument)
            if not token_info:
                return None
            token_id = token_info["token"]

        df         = kite_client.fetch_ohlcv(token_id, today_open)
        rsi_series = indicators.rsi_wilder(df)
        last3      = rsi_series.dropna().iloc[-3:]
        if len(last3) < 3:
            return None
        result = list(last3)
        if cache is not None:
            cache[cache_key] = result
        return result
    except Exception as e:
        print(f"[position_tracker] _get_rsi_snapshot({instrument}): {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Heartbeat — paper-mode implementation
# ──────────────────────────────────────────────────────────────

def run_heartbeat(rsi_cache: dict[tuple[str, str], list[float]] | None = None) -> dict:
    """Paper-trade cycle: process intents, trail SLs, check exits, EOD square-off.

    The consolidated Discord message is always sent/edited at the end of every
    cycle, even when the Kite client is unavailable (positions show stale LTP).

    EOD square-off anchor is 15:10 IST, per paper_engine.is_eod().

    rsi_cache: optional dict threaded through to _get_rsi_snapshot(). main()
    creates one dict and passes the same object into every sub-minute pass so
    RSI lookups are cached across a process's passes without module state.

    Returns a status dict {"open_count": int, "is_eod": bool} so main() can
    drive the sub-minute loop. Existing callers ignore the return value.
    """
    now        = datetime.now(IST)
    date_str   = now.strftime("%Y-%m-%d")
    today_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    live_ltps: dict[str, float] = {}

    paper_engine.get_or_init_daily_capital(date_str)

    # Try to get Kite — if it fails, skip the LTP-dependent steps but still
    # send the Discord update so the consolidated message keeps refreshing.
    kite = None
    try:
        from src import kite_client
        kite = kite_client.get_kite()
    except Exception as e:
        print(f"[position_tracker] heartbeat: cannot get Kite client: {e} — LTP steps skipped")

    if kite is not None:
        # ── Step 1: process fresh intents for all 17 instruments ────────────
        if not paper_engine.entries_blocked(date_str):
            for instrument in _all_instruments():
                intent = _load_tracker_intent(instrument)
                if intent is None:
                    continue

                ts_key = intent.get("ts", "").replace(":", "-").replace(".", "-")
                consumed_key = f"paper:intent_consumed:{instrument.upper()}:{ts_key}"
                if state.redis_exists(consumed_key):
                    continue

                # Mark consumed BEFORE entry attempt to prevent double-fire
                state.redis_set(consumed_key, "1", ex=7200)

                try:
                    paper_engine.simulate_entry(intent, kite)
                except Exception as e:
                    print(f"[position_tracker] {instrument}: simulate_entry error: {e}")

        # ── Step 2: trail SLs and check exits for open paper positions ──────
        for pos in paper_engine.get_open_positions():
            tradingsymbol = pos["tradingsymbol"]
            instrument    = pos["instrument"]
            direction     = pos["direction"]
            entry         = pos["entry_price"]
            T             = pos.get("target_t")
            sl_stage      = pos.get("sl_ladder_stage", pos["initial_sl"])
            asset_class   = pos.get("asset_class", "INDEX")

            exchange = paper_engine._exchange_for(instrument, asset_class)
            ltp_key  = f"{exchange}:{tradingsymbol}"
            try:
                ltp_data = kite.ltp([ltp_key])
                ltp = float(ltp_data.get(ltp_key, {}).get("last_price", 0) or 0)
            except Exception as e:
                print(f"[position_tracker] {tradingsymbol}: LTP fetch failed: {e} — skip cycle")
                continue

            if ltp <= 0:
                # Zero LTP must never be interpreted as an SL hit
                print(f"[position_tracker] {tradingsymbol}: LTP=0 — skip (not an exit)")
                continue

            pos["current_ltp"] = ltp
            live_ltps[tradingsymbol] = ltp

            if T and T > 0:
                # In option-premium space, both CE and PE premiums RISE when the
                # trade is winning. Use the "CE-like" progress formula for both:
                # progress = (ltp - entry) / T.  The original compute_ladder_sl
                # CE branch does exactly this, so pass direction="CE" here.
                raw_progress = (ltp - entry) / T

                rsi3 = _get_rsi_snapshot(instrument, today_open, asset_class=asset_class, cache=rsi_cache)
                market_snapshot = {
                    "rsi_last3":     rsi3,
                    "progress":      raw_progress,
                    "current_price": ltp,
                    "T":             T,
                    "instrument":    instrument,
                }

                # Use "CE" for both directions: SL ratchets up as premium rises
                ladder_sl = compute_ladder_sl(entry, T, ltp, "CE", sl_stage)
                ai_sl     = compute_ai_adjusted_sl(ladder_sl, "CE", market_snapshot)
                final_sl  = compute_final_sl(ladder_sl, ai_sl, "CE")
            else:
                final_sl = sl_stage

            new_stage = round(final_sl, 2)
            prev_stage = pos.get("sl_ladder_stage")
            pos["sl_ladder_stage"] = new_stage
            # Persist only when the ladder actually advanced. current_ltp alone
            # is not worth a write — the Discord embed gets it from live_ltps
            # in Step 4 instead. save_paper_position() writes the position AND
            # re-adds the index entry, so this halves Redis commands per pass
            # on flat ticks.
            if prev_stage is None or new_stage != prev_stage:
                paper_engine.save_paper_position(tradingsymbol, pos)

            # SL hit: option premium dropped to or below the trailing floor
            if ltp <= final_sl:
                print(f"[position_tracker] {tradingsymbol}: SL hit "
                      f"(ltp={ltp:.2f} <= sl={final_sl:.2f})")
                try:
                    paper_engine.simulate_exit(tradingsymbol, final_sl, "sl_hit")
                except Exception as e:
                    print(f"[position_tracker] {tradingsymbol}: simulate_exit error: {e}")

        # ── Step 3: EOD square-off at 15:30 IST ─────────────────────────────
        if paper_engine.is_eod(now):
            for pos in paper_engine.get_open_positions():
                tradingsymbol = pos["tradingsymbol"]
                instrument    = pos["instrument"]
                asset_class   = pos.get("asset_class", "INDEX")

                exchange = paper_engine._exchange_for(instrument, asset_class)
                ltp_key  = f"{exchange}:{tradingsymbol}"
                try:
                    ltp_data = kite.ltp([ltp_key])
                    ltp = float(ltp_data.get(ltp_key, {}).get("last_price", 0) or 0)
                except Exception as e:
                    print(f"[position_tracker] EOD {tradingsymbol}: LTP fetch failed: {e} — using entry")
                    ltp = pos["entry_price"]

                if ltp <= 0:
                    ltp = pos["entry_price"]

                print(f"[position_tracker] EOD square-off: {tradingsymbol} at ltp={ltp:.2f}")
                try:
                    paper_engine.simulate_exit(tradingsymbol, ltp, "eod_squareoff")
                except Exception as e:
                    print(f"[position_tracker] EOD {tradingsymbol}: simulate_exit error: {e}")

            if not paper_engine.eod_posted(date_str):
                closed    = paper_engine.get_closed_positions(date_str)
                total_pnl = paper_engine.get_daily_pnl(date_str)
                trade_notifier.send_paper_eod_summary(closed, total_pnl, date_str)
                paper_engine.mark_eod_posted(date_str)

    # ── Step 4: always post/edit consolidated Discord message ────────────────
    open_now   = paper_engine.get_open_positions()
    closed_now = paper_engine.get_closed_positions(date_str)
    for p in open_now:
        fresh = live_ltps.get(p.get("tradingsymbol"))
        if fresh is not None:
            p["current_ltp"] = fresh
    try:
        trade_notifier.send_paper_consolidated(open_now, closed_now, date_str)
    except Exception as e:
        print(f"[position_tracker] Discord update failed: {e}")

    return {
        "open_count": len(open_now),
        "is_eod":     paper_engine.is_eod(now),
    }


# ──────────────────────────────────────────────────────────────
# Main entrypoint
# ──────────────────────────────────────────────────────────────

def _safe_heartbeat(pass_no: int, rsi_cache: dict[tuple[str, str], list[float]] | None = None) -> dict:
    """Run one heartbeat pass with full exception isolation. A failed pass
    must never abort the remaining passes."""
    try:
        return run_heartbeat(rsi_cache=rsi_cache)
    except Exception as e:
        print(f"[position_tracker] pass {pass_no} FAILED (non-fatal): {e}")
        return {"open_count": -1, "is_eod": False}


def main() -> None:
    subloops, interval, budget = _loop_config()
    started = datetime.now(IST)
    print(f"[position_tracker] Run at {started.isoformat()} "
          f"(subloops={subloops} interval={interval}s budget={budget}s)")

    # One cache dict for the whole process, threaded into every pass so RSI
    # lookups are cached across sub-minute passes without module state.
    rsi_cache: dict[tuple[str, str], list[float]] = {}

    status = _safe_heartbeat(1, rsi_cache)

    if subloops <= 1:
        return

    # Slot schedule is anchored at pass-1 COMPLETION.
    # Budget deadline is anchored at JOB start, so Actions cold start
    # (checkout + pip install) is charged against the 60s cadence.
    slot_base = time.monotonic()
    remaining_budget = budget - _elapsed_since_job_start()
    deadline = time.monotonic() + remaining_budget

    for n in range(2, subloops + 1):
        # open_count == -1 means the pass raised; keep looping, the next pass
        # may recover. Only a confirmed-flat book or EOD stops the loop.
        if status.get("is_eod"):
            print(f"[position_tracker] loop: EOD reached — stopping before pass {n}")
            break
        if status.get("open_count") == 0:
            print(f"[position_tracker] loop: no open positions — stopping before pass {n}")
            break

        next_at = slot_base + (n - 1) * interval
        if next_at > deadline:
            print(f"[position_tracker] loop: budget exhausted "
                  f"({remaining_budget:.1f}s) — skipping passes {n}..{subloops}")
            break

        sleep_for = next_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

        status = _safe_heartbeat(n, rsi_cache)

    # No trailing sleep after the final pass — the process exits immediately.


if __name__ == "__main__":
    main()
