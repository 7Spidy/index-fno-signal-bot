"""IV Rank iron condor paper-trading engine (NIFTY only).

Fully isolated from the existing C1-C4 index/stock signal paths: uses its
own Redis key namespace (condor:*), its own config module (condor_config),
and its own notifier (condor_notifier). Never places real orders — Kite is
used strictly read-only here: quotes, historical data, and the read-only
order_margins/basket_order_margins calculators.

CLI entrypoints:
    python -m src.condor_engine --backfill-vix-history
    python -m src.condor_engine --morning-entry
    python -m src.condor_engine --tracker-tick
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src import condor_config as ccfg
from src import condor_notifier
from src import kite_client
from src import state
from src.position_tracker import compute_ladder_sl

IST = ZoneInfo("Asia/Kolkata")

NIFTY = "NIFTY"


# ──────────────────────────────────────────────────────────────
# VIX history backfill (idempotent)
# ──────────────────────────────────────────────────────────────

def _load_vix_history() -> dict:
    raw = state.redis_get(ccfg.REDIS_VIX_HISTORY_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_vix_history(history: dict) -> None:
    state.redis_set(ccfg.REDIS_VIX_HISTORY_KEY, json.dumps(history))


def _history_is_sufficient(history: dict, today: date) -> bool:
    """True if the stored history already covers >= IV_RANK_WINDOW_DAYS
    trading days ending recently (within the last 5 calendar days)."""
    if len(history) < ccfg.IV_RANK_WINDOW_DAYS:
        return False
    latest = max(date.fromisoformat(d) for d in history.keys())
    return (today - latest).days <= 5


def _find_vix_instrument_token(kite) -> int | None:
    instruments = kite.instruments(ccfg.VIX_EXCHANGE)
    for inst in instruments:
        if inst.get("tradingsymbol") == ccfg.VIX_TRADINGSYMBOL:
            return inst["instrument_token"]
    return None


def backfill_vix_history(kite=None) -> None:
    """Idempotent one-time (safe repeated) backfill of daily India VIX
    closes into REDIS_VIX_HISTORY_KEY. Merges by date — never blind-appends."""
    today = datetime.now(IST).date()
    history = _load_vix_history()

    if _history_is_sufficient(history, today):
        print(f"[condor_engine] VIX history sufficient ({len(history)} days) — skipping backfill")
        return

    kite = kite or kite_client.get_kite()
    token = _find_vix_instrument_token(kite)
    if token is None:
        print(f"[condor_engine] backfill: could not resolve {ccfg.VIX_TRADINGSYMBOL} instrument token")
        return

    from_date = datetime.now(IST) - timedelta(days=ccfg.IV_RANK_WINDOW_DAYS + 30)
    to_date = datetime.now(IST)
    try:
        data = kite.historical_data(
            instrument_token=token,
            from_date=from_date,
            to_date=to_date,
            interval="day",
            continuous=False,
            oi=False,
        )
    except Exception as e:
        print(f"[condor_engine] backfill: historical_data failed: {e}")
        return

    added = 0
    for row in data:
        d = row["date"]
        d_str = d.date().isoformat() if hasattr(d, "date") else str(d)[:10]
        close = row.get("close")
        if close is None:
            continue
        if d_str not in history:
            added += 1
        history[d_str] = float(close)

    _save_vix_history(history)
    print(f"[condor_engine] VIX history backfilled: {added} new day(s), {len(history)} total")


# ──────────────────────────────────────────────────────────────
# IV Rank
# ──────────────────────────────────────────────────────────────

def compute_iv_rank(history: dict, today_vix: float) -> tuple[float, bool]:
    """Returns (iv_rank, short_window). short_window=True means history has
    fewer than IV_RANK_WINDOW_DAYS entries — rank is still computed and used,
    but the caller must surface the reliability warning."""
    dates_sorted = sorted(history.keys())
    tail = [history[d] for d in dates_sorted[-(ccfg.IV_RANK_WINDOW_DAYS - 1):]]
    window = tail + [today_vix]
    short_window = len(window) < ccfg.IV_RANK_WINDOW_DAYS

    lo, hi = min(window), max(window)
    if hi == lo:
        return 0.0, short_window
    iv_rank = (today_vix - lo) / (hi - lo) * 100
    return iv_rank, short_window


def get_live_vix(kite) -> float | None:
    key = f"{ccfg.VIX_EXCHANGE}:{ccfg.VIX_TRADINGSYMBOL}"
    try:
        data = kite.quote([key])
        return float(data[key]["last_price"])
    except Exception as e:
        print(f"[condor_engine] get_live_vix failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Position lock / state helpers
# ──────────────────────────────────────────────────────────────

def _is_locked() -> bool:
    return state.redis_exists(ccfg.REDIS_CONDOR_LOCK)


def _load_position() -> dict | None:
    raw = state.redis_get(ccfg.REDIS_CONDOR_POSITION)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_position(position: dict) -> None:
    state.redis_set(ccfg.REDIS_CONDOR_POSITION, json.dumps(position))


def _clear_position() -> None:
    state.redis_delete(ccfg.REDIS_CONDOR_POSITION)
    state.redis_delete(ccfg.REDIS_CONDOR_LOCK)


# ──────────────────────────────────────────────────────────────
# Liquidity-aware strike scan
# ──────────────────────────────────────────────────────────────

def _find_option(chain: list[dict], strike: float, opt_type: str) -> dict | None:
    for inst in chain:
        if inst["instrument_type"] == opt_type and inst["strike"] == strike:
            return inst
    return None


def _scan_short_leg(kite, chain: list[dict], opt_type: str,
                     start_strike: float, step_direction: int) -> dict | None:
    """Scans outward (in STRIKE_STEP increments, up to MAX_STRIKE_SCAN_STEPS
    retries) for a liquid, monotonically-ordered short-leg strike.

    step_direction: +1 for calls (further OTM = higher strike),
                    -1 for puts  (further OTM = lower strike).

    Rejects a candidate if its OI < MIN_OI_LOTS, or if its premium is not
    strictly greater than its further-OTM neighbor's premium (both call and
    put premiums decay moving further OTM, so "self > further-OTM neighbor"
    is the correct monotonic check for either side).
    """
    strike = start_strike
    for _ in range(ccfg.MAX_STRIKE_SCAN_STEPS + 1):
        inst = _find_option(chain, strike, opt_type)
        neighbor_strike = strike + ccfg.STRIKE_STEP * step_direction
        neighbor = _find_option(chain, neighbor_strike, opt_type)

        if inst and neighbor:
            key_self = f"NFO:{inst['tradingsymbol']}"
            key_neigh = f"NFO:{neighbor['tradingsymbol']}"
            try:
                quotes = kite.quote([key_self, key_neigh])
            except Exception as e:
                print(f"[condor_engine] liquidity scan quote failed: {e}")
                quotes = {}

            q_self = quotes.get(key_self, {})
            q_neigh = quotes.get(key_neigh, {})
            oi_self = q_self.get("oi") or 0
            ltp_self = q_self.get("last_price")
            ltp_neigh = q_neigh.get("last_price")

            if (oi_self >= ccfg.MIN_OI_LOTS
                    and ltp_self is not None
                    and ltp_neigh is not None
                    and ltp_self > ltp_neigh):
                return {
                    "tradingsymbol": inst["tradingsymbol"],
                    "token": inst["instrument_token"],
                    "strike": strike,
                    "ltp": float(ltp_self),
                }

        strike += ccfg.STRIKE_STEP * step_direction
    return None


# ──────────────────────────────────────────────────────────────
# Short-leg trailing SL — direction-inverted vs the long-option ladder
# ──────────────────────────────────────────────────────────────

def _short_leg_trailing_sl(entry_premium: float, target: float,
                            current_premium: float, prior_sl: float) -> float:
    """Trailing stop for a SHORT option leg.

    The existing compute_ladder_sl() in position_tracker.py assumes a LONG
    option: it profits as price rises (CE) or falls (PE) relative to entry.
    A SHORT leg profits as its OWN premium falls (regardless of CE/PE) — the
    opposite frame. We must NOT feed the short leg's real CE/PE type into
    compute_ladder_sl (that would track the wrong direction).

    Instead we reuse compute_ladder_sl's PE branch as a synthetic mapping:
    PE progress = (entry - current_price) / T, which is exactly
    "premium falling = progress" — the correct short-leg frame. Its SL
    formula (entry - fraction*T, monotonically non-increasing via min())
    ratchets the buy-back trigger DOWN as more decay is captured, which is
    the correct "lock in more profit, never loosen" behavior for a short
    leg being tracked toward a target buy-back price.
    """
    return compute_ladder_sl(entry_premium, target, current_premium, "PE", prior_sl)


# ──────────────────────────────────────────────────────────────
# Morning entry
# ──────────────────────────────────────────────────────────────

def morning_entry(kite=None) -> None:
    if _is_locked():
        print("[condor_engine] condor already open — skipping entry")
        return

    kite = kite or kite_client.get_kite()

    history = _load_vix_history()
    today_vix = get_live_vix(kite)
    if today_vix is None:
        print("[condor_engine] morning_entry: could not fetch live VIX — abort")
        return

    iv_rank, short_window = compute_iv_rank(history, today_vix)
    if short_window:
        print(f"[condor_engine] WARNING: IV Rank computed on a short window "
              f"({len(history)} days < {ccfg.IV_RANK_WINDOW_DAYS}) — less reliable")

    if iv_rank < ccfg.IV_RANK_THRESHOLD:
        reason = f"IV Rank {iv_rank:.1f} < gate {ccfg.IV_RANK_THRESHOLD}"
        if short_window:
            reason += " (short history window — less reliable)"
        print(f"[condor_engine] entry skipped: {reason}")
        condor_notifier.send_skip(iv_rank, reason)
        return

    spot = kite_client.get_spot_ltp(NIFTY)
    if not spot:
        print("[condor_engine] morning_entry: could not fetch NIFTY spot — abort")
        return

    expiry, _rolled = kite_client.get_nearest_expiry(NIFTY)

    exchange = "NFO"
    chain = [i for i in kite.instruments(exchange)
             if i["name"] == NIFTY
             and i["instrument_type"] in ("CE", "PE")
             and i["expiry"] == expiry]

    atm = round(spot / ccfg.STRIKE_STEP) * ccfg.STRIKE_STEP
    short_call_candidate = atm + ccfg.SHORT_OTM_OFFSET_PTS
    short_put_candidate = atm - ccfg.SHORT_OTM_OFFSET_PTS

    short_call = _scan_short_leg(kite, chain, "CE", short_call_candidate, +1)
    if short_call is None:
        reason = "no clean strikes (liquidity) — call side"
        print(f"[condor_engine] entry aborted: {reason}")
        condor_notifier.send_skip(iv_rank, reason)
        return

    short_put = _scan_short_leg(kite, chain, "PE", short_put_candidate, -1)
    if short_put is None:
        reason = "no clean strikes (liquidity) — put side"
        print(f"[condor_engine] entry aborted: {reason}")
        condor_notifier.send_skip(iv_rank, reason)
        return

    long_call_strike = short_call["strike"] + ccfg.SPREAD_WIDTH_PTS
    long_put_strike = short_put["strike"] - ccfg.SPREAD_WIDTH_PTS
    long_call_inst = _find_option(chain, long_call_strike, "CE")
    long_put_inst = _find_option(chain, long_put_strike, "PE")
    if long_call_inst is None or long_put_inst is None:
        reason = "no clean strikes (liquidity) — long wing not found"
        print(f"[condor_engine] entry aborted: {reason}")
        condor_notifier.send_skip(iv_rank, reason)
        return

    leg_keys = {
        "short_call": f"NFO:{short_call['tradingsymbol']}",
        "long_call": f"NFO:{long_call_inst['tradingsymbol']}",
        "short_put": f"NFO:{short_put['tradingsymbol']}",
        "long_put": f"NFO:{long_put_inst['tradingsymbol']}",
    }
    try:
        quotes = kite.quote(list(leg_keys.values()))
    except Exception as e:
        print(f"[condor_engine] entry aborted: leg quote fetch failed: {e}")
        return

    def _ltp(key: str) -> float | None:
        q = quotes.get(key)
        return float(q["last_price"]) if q and q.get("last_price") is not None else None

    short_call_ltp = _ltp(leg_keys["short_call"])
    long_call_ltp = _ltp(leg_keys["long_call"])
    short_put_ltp = _ltp(leg_keys["short_put"])
    long_put_ltp = _ltp(leg_keys["long_put"])
    if None in (short_call_ltp, long_call_ltp, short_put_ltp, long_put_ltp):
        reason = "missing live LTP on one or more legs"
        print(f"[condor_engine] entry aborted: {reason}")
        condor_notifier.send_skip(iv_rank, reason)
        return

    net_credit_pts = (short_call_ltp - long_call_ltp) + (short_put_ltp - long_put_ltp)
    if net_credit_pts <= 0:
        reason = "entry not a net credit"
        print(f"[condor_engine] entry aborted: {reason} (net_credit_pts={net_credit_pts:.2f})")
        condor_notifier.send_skip(iv_rank, reason)
        return

    # ── Sizing via real (read-only) basket-margin API ──
    basket = [
        {"exchange": "NFO", "tradingsymbol": short_call["tradingsymbol"],
         "transaction_type": "SELL", "variety": "regular", "product": "NRML",
         "order_type": "MARKET", "quantity": ccfg.NIFTY_LOT_SIZE},
        {"exchange": "NFO", "tradingsymbol": long_call_inst["tradingsymbol"],
         "transaction_type": "BUY", "variety": "regular", "product": "NRML",
         "order_type": "MARKET", "quantity": ccfg.NIFTY_LOT_SIZE},
        {"exchange": "NFO", "tradingsymbol": short_put["tradingsymbol"],
         "transaction_type": "SELL", "variety": "regular", "product": "NRML",
         "order_type": "MARKET", "quantity": ccfg.NIFTY_LOT_SIZE},
        {"exchange": "NFO", "tradingsymbol": long_put_inst["tradingsymbol"],
         "transaction_type": "BUY", "variety": "regular", "product": "NRML",
         "order_type": "MARKET", "quantity": ccfg.NIFTY_LOT_SIZE},
    ]
    margin_per_lot = _compute_basket_margin(kite, basket)
    if margin_per_lot is None or margin_per_lot <= 0:
        reason = "margin calculation failed"
        print(f"[condor_engine] entry aborted: {reason}")
        condor_notifier.send_skip(iv_rank, reason)
        return

    available_capital = ccfg.CONDOR_CAPITAL * (1 - ccfg.CAPITAL_RESERVE_FRAC)
    if margin_per_lot > available_capital:
        reason = "insufficient capital for 1 lot"
        print(f"[condor_engine] entry aborted: {reason} "
              f"(margin/lot={margin_per_lot:.0f}, capital={available_capital:.0f})")
        condor_notifier.send_skip(iv_rank, reason)
        return

    lots = max(1, math.floor(available_capital / margin_per_lot))
    capital_deployed = lots * margin_per_lot

    T_short_call = ccfg.SHORT_LEG_TARGET_CAPTURE_FRAC * short_call_ltp
    T_short_put = ccfg.SHORT_LEG_TARGET_CAPTURE_FRAC * short_put_ltp

    position = {
        "expiry": expiry.isoformat(),
        "iv_rank_entry": round(iv_rank, 1),
        "net_credit_pts": round(net_credit_pts, 2),
        "lots": lots,
        "margin_per_lot": round(margin_per_lot, 2),
        "capital_deployed": round(capital_deployed, 2),
        "entry_ts": datetime.now(IST).isoformat(),
        "legs": {
            "short_call": {
                "side": "SELL", "type": "CE",
                "tradingsymbol": short_call["tradingsymbol"],
                "strike": short_call["strike"],
                "entry_premium": round(short_call_ltp, 2),
                "sl": round(short_call_ltp, 2),
                "target": round(T_short_call, 2),
            },
            "long_call": {
                "side": "BUY", "type": "CE",
                "tradingsymbol": long_call_inst["tradingsymbol"],
                "strike": long_call_strike,
                "entry_premium": round(long_call_ltp, 2),
                "sl": None,
                "target": None,
            },
            "short_put": {
                "side": "SELL", "type": "PE",
                "tradingsymbol": short_put["tradingsymbol"],
                "strike": short_put["strike"],
                "entry_premium": round(short_put_ltp, 2),
                "sl": round(short_put_ltp, 2),
                "target": round(T_short_put, 2),
            },
            "long_put": {
                "side": "BUY", "type": "PE",
                "tradingsymbol": long_put_inst["tradingsymbol"],
                "strike": long_put_strike,
                "entry_premium": round(long_put_ltp, 2),
                "sl": None,
                "target": None,
            },
        },
    }

    _save_position(position)
    state.redis_set(ccfg.REDIS_CONDOR_LOCK, "1")

    legs_for_embed = [
        {"side": lg["side"], "tradingsymbol": lg["tradingsymbol"],
         "ltp": lg["entry_premium"], "sl": lg["sl"], "t": lg["target"]}
        for lg in position["legs"].values()
    ]
    condor_notifier.send_entry(position, legs_for_embed, pnl_rs=0.0)
    print(f"[condor_engine] ENTRY: IV Rank={iv_rank:.1f} lots={lots} "
          f"capital=₹{capital_deployed:.0f} net_credit={net_credit_pts:.2f}pts")


def _compute_basket_margin(kite, basket: list[dict]) -> float | None:
    """Calls the read-only basket-margin calculator (does NOT place any
    order). Returns the total margin required for the basket as quoted, or
    None on failure. Tolerant of a couple of plausible response shapes since
    exact SDK response formatting can vary by kiteconnect version."""
    try:
        resp = kite.basket_order_margins(basket)
    except Exception as e:
        print(f"[condor_engine] basket_order_margins failed: {e}")
        return None

    try:
        if isinstance(resp, dict):
            final = resp.get("final") or resp.get("initial")
            if isinstance(final, dict) and "total" in final:
                return float(final["total"])
            if "total" in resp:
                return float(resp["total"])
        if isinstance(resp, list):
            return float(sum(o.get("total", 0) for o in resp))
    except Exception as e:
        print(f"[condor_engine] basket margin response parse failed: {e}")
        return None

    print(f"[condor_engine] unrecognized basket margin response shape: {resp!r}")
    return None


# ──────────────────────────────────────────────────────────────
# Tracker tick
# ──────────────────────────────────────────────────────────────

def tracker_tick(kite=None) -> None:
    if not _is_locked():
        return  # cheap no-op — no open position

    position = _load_position()
    if position is None:
        print("[condor_engine] tracker_tick: lock set but no position found — clearing lock")
        state.redis_delete(ccfg.REDIS_CONDOR_LOCK)
        return

    kite = kite or kite_client.get_kite()
    legs = position["legs"]
    keys = {name: f"NFO:{lg['tradingsymbol']}" for name, lg in legs.items()}
    try:
        quotes = kite.quote(list(keys.values()))
    except Exception as e:
        print(f"[condor_engine] tracker_tick: quote fetch failed: {e}")
        return

    def _now(name: str) -> float | None:
        q = quotes.get(keys[name])
        return float(q["last_price"]) if q and q.get("last_price") is not None else None

    now_prices = {name: _now(name) for name in legs}
    if any(v is None for v in now_prices.values()):
        print("[condor_engine] tracker_tick: missing LTP on one or more legs — skip cycle")
        return

    # Update trailing SL on SHORT legs only; LONG wings are static.
    for name in ("short_call", "short_put"):
        lg = legs[name]
        new_sl = _short_leg_trailing_sl(
            lg["entry_premium"], lg["target"], now_prices[name], lg["sl"],
        )
        lg["sl"] = round(new_sl, 2)

    pnl_pts = position["net_credit_pts"] - (
        (now_prices["short_call"] - now_prices["long_call"])
        + (now_prices["short_put"] - now_prices["long_put"])
    )
    lots = position["lots"]
    pnl_rs = pnl_pts * ccfg.NIFTY_LOT_SIZE * lots

    T_total = legs["short_call"]["target"] + legs["short_put"]["target"]

    exit_reason = None
    if now_prices["short_call"] >= legs["short_call"]["sl"] or now_prices["short_put"] >= legs["short_put"]["sl"]:
        exit_reason = "trailing_sl"
    elif pnl_pts >= T_total:
        exit_reason = "target"

    legs_for_embed = [
        {"side": lg["side"], "tradingsymbol": lg["tradingsymbol"],
         "ltp": now_prices[name], "sl": lg["sl"], "t": lg["target"]}
        for name, lg in legs.items()
    ]

    if exit_reason:
        print(f"[condor_engine] EXIT ({exit_reason}): pnl_rs={pnl_rs:.2f}")
        condor_notifier.send_close(position, legs_for_embed, pnl_rs, exit_reason)
        _clear_position()
        return

    _save_position(position)
    condor_notifier.send_update(position, legs_for_embed, pnl_rs)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill-vix-history", action="store_true")
    parser.add_argument("--morning-entry", action="store_true")
    parser.add_argument("--tracker-tick", action="store_true")
    args = parser.parse_args()

    if args.backfill_vix_history:
        backfill_vix_history()
    if args.morning_entry:
        morning_entry()
    if args.tracker_tick:
        tracker_tick()


if __name__ == "__main__":
    main()
