"""
Trailing SL position tracker — advisory only, never places/modifies orders.

Triggered every minute by cron-job.org → workflow_dispatch on trade-tracker.yml.

Flow (pull-based, no manual Discord commands):
  1. Call get_positions() via Kite every cycle.
  2. Discover any open F&O position (index in config.INSTRUMENTS or stock in
     stock_config.STOCKS) not already tracked in Redis.
  3. Require a position to be seen on 2 consecutive heartbeats (same
     tradingsymbol, nonzero qty) before posting a "position detected" alert
     and starting SL ladder tracking — guards against a stale/transient
     Kite response.
  4. On confirmation, reconstruct entry context (SL, target T) from the most
     recent same-instrument signal intent written to Redis — either the
     legacy NIFTY-only executor_bridge keys (`executor:pending_intent` /
     `executor:position`) or the per-instrument tracker_bridge key
     (`tracker:pending_intent:{INSTRUMENT}`), not from Discord history —
     these Redis keys already carry the structural SL and target used to
     build the alert, and are cheaper/more reliable than a message scan.
  5. Track the SL ladder per open position, keyed by tradingsymbol so
     concurrent positions (any instrument/direction) never collide.
  6. Detect exits purely via a qty -> 0 (or position-disappears) transition
     and post a P&L summary, labelled ladder-driven or manual/untracked.
  7. `position:{tradingsymbol}` Redis keys expire at the next 16:00 IST.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src import config
from src import state
from src import stock_config
from src import trade_notifier

IST = ZoneInfo("Asia/Kolkata")

INDEX_KEY = "position:index"

# ──────────────────────────────────────────────────────────────
# Ladder / SL computation
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

    # How far has price moved toward the target, as a fraction of T?
    if direction == "CE":
        progress = (current_price - entry) / T
    else:
        progress = (entry - current_price) / T

    if progress < 0.5:
        # No ladder step reached yet — hold original SL
        return prior_sl

    if progress < 0.9:
        sl_fraction = 0.25
    elif progress < 1.0:
        sl_fraction = 0.60
    else:
        # progress >= 1.0: base fraction is 0.9, then +0.1 per additional 0.1T
        # Round before floor to avoid floating-point drift (e.g. 1.2-1.0=0.19999…)
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

    A more sophisticated version (multi-factor scoring, adaptive tightening)
    is a planned future iteration — not part of this change.
    """
    direction = direction.upper()
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")

    rsi_values = market_snapshot.get("rsi_last3")   # list of 3 RSI floats, oldest first
    progress   = market_snapshot.get("progress", 0.0)
    current_price = market_snapshot.get("current_price")
    T = market_snapshot.get("T")

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

    # Tighten: bring SL very close to current price
    if direction == "CE":
        tightened = current_price - 0.05 * T
        return max(tightened, ladder_sl)   # may only tighten (i.e. raise)
    else:
        tightened = current_price + 0.05 * T
        return min(tightened, ladder_sl)   # may only tighten (i.e. lower)


def compute_final_sl(ladder_sl: float, ai_sl: float, direction: str) -> float:
    """Combine ladder SL and AI-adjusted SL, always taking the tighter side."""
    direction = direction.upper()
    if direction == "CE":
        return max(ladder_sl, ai_sl)
    return min(ladder_sl, ai_sl)


# ──────────────────────────────────────────────────────────────
# Redis state model — position:{tradingsymbol}, keyed independently
# so concurrent NIFTY CE + PE positions never collide.
# ──────────────────────────────────────────────────────────────

def _seconds_until_next_1600_ist() -> int:
    """TTL target: next occurrence of 16:00 IST (today if not yet past, else tomorrow)."""
    now = datetime.now(IST)
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

_INDEX_NAMES = [inst["name"] for inst in config.INSTRUMENTS]          # NIFTY, BANKNIFTY, SENSEX
_STOCK_NAMES = list(stock_config.STOCK_BY_NAME.keys())                # 14 stocks

# Longest-name-first so no name can shadow a longer one that happens to
# share a prefix.
_KNOWN_UNDERLYINGS = sorted(set(_INDEX_NAMES) | set(_STOCK_NAMES), key=len, reverse=True)


def _underlying_from_tradingsymbol(tradingsymbol: str) -> str | None:
    """Extract underlying name from a Kite tradingsymbol — matches any
    INDEX (config.INSTRUMENTS) or STOCK (stock_config.STOCKS) name.

    Examples: NIFTY26JUN24600CE → NIFTY, MARUTI26JUL14300CE → MARUTI
    """
    sym = tradingsymbol.upper()
    for name in _KNOWN_UNDERLYINGS:
        if sym.startswith(name):
            return name
    return None


def _asset_class_for(instrument: str) -> str:
    """INDEX for config.INSTRUMENTS names, STOCK for stock_config.STOCKS
    names, UNKNOWN otherwise (should not happen for anything that passed
    _underlying_from_tradingsymbol, but never raises)."""
    if instrument in _INDEX_NAMES:
        return "INDEX"
    if instrument in stock_config.STOCK_BY_NAME:
        return "STOCK"
    return "UNKNOWN"


def _direction_from_tradingsymbol(tradingsymbol: str) -> str | None:
    sym = tradingsymbol.upper()
    if sym.endswith("CE"):
        return "CE"
    if sym.endswith("PE"):
        return "PE"
    return None


# ──────────────────────────────────────────────────────────────
# Intent payload lookup — this IS the "alert" reconstruction source.
#
# executor_bridge.py (this repo) writes executor:pending_intent whenever a
# signal fires; the (separate) auto-executor writes executor:position once
# it opens a position. Both are cheaper and more reliable than scanning
# Discord history, so they are used in preference to any message scan.
# ──────────────────────────────────────────────────────────────

def _load_tracker_intent(instrument: str) -> dict | None:
    """Look up the per-instrument tracker-intent key
    (tracker:pending_intent:{INSTRUMENT}), written by tracker_bridge.py."""
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
    """Look up the most recent Redis intent payload for this instrument.

    Priority order:
      1. Legacy global executor keys (executor:pending_intent /
         executor:position) — these reflect the real, external
         auto-executor's actual state and are NIFTY-only in practice, but
         are checked for ALL instruments for backward compatibility.
      2. Per-instrument tracker key (tracker:pending_intent:{instrument}) —
         covers every instrument this bot tracks, including NIFTY when the
         legacy keys are absent/stale/expired.

    Returns the parsed dict if found, else None.
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
# Kite SL order / exit price lookups
# ──────────────────────────────────────────────────────────────

def _get_kite_sl_for(kite, tradingsymbol: str) -> float | None:
    """Find the current SL order price in Kite for this tradingsymbol.

    Returns None if no SL order is found.
    """
    try:
        orders = kite.orders()
        sl_types = {"SL", "SL-M"}
        for order in orders:
            if (
                order.get("tradingsymbol") == tradingsymbol
                and order.get("order_type") in sl_types
                and order.get("status") in ("TRIGGER PENDING", "OPEN")
            ):
                return float(order.get("trigger_price") or order.get("price") or 0)
        return None
    except Exception as e:
        print(f"[position_tracker] _get_kite_sl_for({tradingsymbol}): {e}")
        return None


def _get_kite_exit_price(kite, tradingsymbol: str) -> float | None:
    """Average fill price across completed SELL orders for this tradingsymbol today.

    Uses get_orders() (today's order book) rather than get_trades() — positions
    never span multiple days given the daily TTL reset, so today's orders are
    sufficient. Returns None if no completed SELL fills are found.
    """
    try:
        orders = kite.orders()
    except Exception as e:
        print(f"[position_tracker] _get_kite_exit_price({tradingsymbol}): {e}")
        return None

    sell_fills = [
        o for o in orders
        if o.get("tradingsymbol") == tradingsymbol
        and o.get("transaction_type") == "SELL"
        and o.get("status") == "COMPLETE"
    ]
    if not sell_fills:
        return None

    total_qty = sum(o.get("filled_quantity") or o.get("quantity") or 0 for o in sell_fills)
    if total_qty == 0:
        return None
    total_value = sum(
        (o.get("average_price") or 0) * (o.get("filled_quantity") or o.get("quantity") or 0)
        for o in sell_fills
    )
    return total_value / total_qty


def _is_ladder_driven(exit_price: float, sl_ladder_stage: float | None) -> bool:
    """Whether the exit price matches the last known ladder SL within tolerance."""
    if sl_ladder_stage is None:
        return False
    tolerance = max(abs(sl_ladder_stage) * 0.02, 0.5)
    return abs(exit_price - sl_ladder_stage) <= tolerance


def _get_rsi_snapshot(instrument: str, today_open: datetime, asset_class: str = "INDEX") -> list[float] | None:
    """Fetch last 3 RSI values for the instrument.

    INDEX: futures token from kite:instrument_tokens ({instrument: {"token": ...}}).
    STOCK: equity token from stock_config.REDIS_EQUITY_TOKENS_KEY ({instrument: token_id}),
           mirroring how stock_main._fetch_and_evaluate sources its own RSI.

    Returns [r_oldest, r_middle, r_newest] or None on failure.
    """
    try:
        from src import kite_client, indicators, state as st

        if asset_class == "STOCK":
            raw = st.redis_get(stock_config.REDIS_EQUITY_TOKENS_KEY)
            if not raw:
                return None
            tokens = json.loads(raw)
            token_id = tokens.get(instrument)
            if not token_id:
                return None
        else:
            raw = st.redis_get("kite:instrument_tokens")
            if not raw:
                return None
            tokens = json.loads(raw)
            token_info = tokens.get(instrument)
            if not token_info:
                return None
            token_id = token_info["token"]

        df = kite_client.fetch_ohlcv(token_id, today_open)
        rsi_series = indicators.rsi_wilder(df)
        last3 = rsi_series.dropna().iloc[-3:]
        if len(last3) < 3:
            return None
        return list(last3)
    except Exception as e:
        print(f"[position_tracker] _get_rsi_snapshot({instrument}): {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Discovery / confirmation / ongoing tracking / exit — per tradingsymbol
# ──────────────────────────────────────────────────────────────

def _handle_new_sighting(tradingsymbol: str, pos: dict) -> None:
    """First sighting of an open tracked F&O position — pending, no alert yet."""
    instrument = _underlying_from_tradingsymbol(tradingsymbol)
    if instrument is None:
        print(f"[position_tracker] {tradingsymbol}: cannot determine underlying — skip")
        return

    direction = _direction_from_tradingsymbol(tradingsymbol)
    if direction is None:
        print(f"[position_tracker] {tradingsymbol}: cannot determine CE/PE — skip")
        return

    asset_class = _asset_class_for(instrument)
    data = {
        "tradingsymbol":       tradingsymbol,
        "instrument":          instrument,
        "asset_class":         asset_class,
        "direction":           direction,
        "entry_price":         pos.get("average_price") or 0.0,
        "sl":                  None,
        "target_t":            None,
        "entry_alert_ts":      None,
        "discovered_at":       datetime.now(IST).isoformat(),
        "sl_ladder_stage":     None,
        "qty":                 pos.get("quantity", 0),
        "confirm_count":       1,
        "action_alerts_sent":  0,
        "action_alerts_acked": 0,
    }
    _save_position(tradingsymbol, data)
    print(f"[position_tracker] {tradingsymbol}: new sighting ({instrument}/{asset_class}), "
          f"qty={data['qty']} — confirm_count=1, no alert yet")


def _handle_confirm(kite, tradingsymbol: str, pos: dict, existing: dict) -> None:
    """Second consecutive sighting — confirm, match alert intent, start ladder."""
    instrument = existing["instrument"]
    direction  = existing["direction"]
    entry_price = pos.get("average_price") or existing.get("entry_price") or 0.0
    qty = pos.get("quantity", 0)

    intent = _load_intent(instrument)
    T: float | None = None
    sl: float | None = None
    entry_alert_ts: str | None = None

    if intent:
        T = intent.get("target_pts")
        if T is None:
            risk_pts  = intent.get("spot_risk_pts")
            target_rr = intent.get("target_rr")
            if risk_pts and target_rr:
                T = risk_pts * target_rr
        sl = intent.get("spot_sl")
        entry_alert_ts = intent.get("ts")
        print(f"[position_tracker] {tradingsymbol}: matched alert intent — T={T}, sl={sl}")
    else:
        print(f"[position_tracker] {tradingsymbol}: no matching alert intent — "
              f"SL/T unavailable, raw P&L tracking only")
        sl = _get_kite_sl_for(kite, tradingsymbol)

    sl_for_display = sl
    if sl is None:
        sl = entry_price  # fall back to entry so the ladder has a floor

    existing.update({
        "entry_price":      entry_price,
        "sl":                sl,
        "target_t":          T,
        "entry_alert_ts":    entry_alert_ts,
        "sl_ladder_stage":   sl,
        "qty":               qty,
        "confirm_count":     2,
    })
    _save_position(tradingsymbol, existing)

    trade_notifier.send_position_detected(
        instrument=instrument,
        direction=direction,
        tradingsymbol=tradingsymbol,
        entry_price=entry_price,
        sl=sl_for_display,
        target_t=T,
        qty=qty,
    )
    print(f"[position_tracker] {tradingsymbol}: confirmed at confirm_count=2 — "
          f"tracking + SL ladder started")


def _handle_ongoing(kite, tradingsymbol: str, pos: dict, existing: dict, today_open: datetime) -> None:
    """Normal per-heartbeat tracking cycle for an already-confirmed position."""
    instrument = existing["instrument"]
    direction  = existing["direction"]
    entry      = existing["entry_price"]
    T          = existing.get("target_t")
    sl_ladder_stage = existing.get("sl_ladder_stage")
    if sl_ladder_stage is None:
        sl_ladder_stage = entry

    qty      = pos.get("quantity", 0)
    prev_qty = existing.get("qty", qty)

    if qty > prev_qty:
        # Averaging in — Kite's blended average_price is authoritative.
        # SL / target_t / sl_ladder_stage stay fixed at original entry.
        entry = pos.get("average_price") or entry
        print(f"[position_tracker] {tradingsymbol}: qty increased {prev_qty} -> {qty} (averaging in)")
    elif qty < prev_qty:
        trade_notifier.send_partial_exit(instrument, direction, tradingsymbol, prev_qty, qty)
        print(f"[position_tracker] {tradingsymbol}: partial exit detected {prev_qty} -> {qty}")

    try:
        ltp_data = kite.ltp([f"NFO:{tradingsymbol}"])
        ltp = float(ltp_data.get(f"NFO:{tradingsymbol}", {}).get("last_price", 0) or 0)
        if ltp == 0:
            print(f"[position_tracker] heartbeat: LTP=0 for {tradingsymbol} — skip")
            existing["entry_price"] = entry
            existing["qty"] = qty
            _save_position(tradingsymbol, existing)
            return
    except Exception as e:
        print(f"[position_tracker] heartbeat: LTP fetch failed for {tradingsymbol}: {e}")
        return

    if T and T > 0:
        if direction == "CE":
            raw_progress = (ltp - entry) / T
        else:
            raw_progress = (entry - ltp) / T
        progress_pct = raw_progress * 100.0
    else:
        raw_progress = 0.0
        progress_pct = 0.0

    rsi3 = _get_rsi_snapshot(instrument, today_open, asset_class=existing.get("asset_class", "INDEX"))
    market_snapshot = {
        "rsi_last3":     rsi3,
        "progress":      raw_progress,
        "current_price": ltp,
        "T":             T,
        "instrument":    instrument,
    }

    if T and T > 0:
        ladder_sl = compute_ladder_sl(entry, T, ltp, direction, sl_ladder_stage)
        ai_sl     = compute_ai_adjusted_sl(ladder_sl, direction, market_snapshot)
        final_sl  = compute_final_sl(ladder_sl, ai_sl, direction)
    else:
        # T unknown — can't ladder; hold at last known stage
        final_sl = sl_ladder_stage

    kite_sl = _get_kite_sl_for(kite, tradingsymbol)

    action_needed = False
    if kite_sl is not None:
        if direction == "CE" and kite_sl < final_sl:
            action_needed = True
        elif direction == "PE" and kite_sl > final_sl:
            action_needed = True

    action_alerts_sent  = existing.get("action_alerts_sent", 0)
    action_alerts_acked = existing.get("action_alerts_acked", 0)

    if action_needed:
        sent = trade_notifier.send_action(
            instrument=instrument,
            direction=direction,
            ltp=ltp,
            progress_pct=progress_pct,
            current_sl_kite=kite_sl,
            required_sl=final_sl,
        )
        if sent:
            action_alerts_sent += 1
    else:
        trade_notifier.send_fyi(
            instrument=instrument,
            direction=direction,
            ltp=ltp,
            progress_pct=progress_pct,
            current_sl=kite_sl if kite_sl is not None else final_sl,
        )
        if action_alerts_sent > action_alerts_acked and kite_sl is not None:
            if direction == "CE" and kite_sl >= final_sl:
                action_alerts_acked = action_alerts_sent
            elif direction == "PE" and kite_sl <= final_sl:
                action_alerts_acked = action_alerts_sent

    existing.update({
        "entry_price":         entry,
        "qty":                 qty,
        "sl_ladder_stage":     final_sl,
        "action_alerts_sent":  action_alerts_sent,
        "action_alerts_acked": action_alerts_acked,
    })
    _save_position(tradingsymbol, existing)


def _finalize_exit(kite, tradingsymbol: str, existing: dict, last_pos_snapshot: dict | None) -> None:
    """Position fully closed (qty -> 0) — post P&L summary and drop tracking."""
    instrument = existing["instrument"]
    direction  = existing["direction"]
    entry      = existing["entry_price"]
    qty        = existing.get("qty", 0)
    sl_ladder_stage = existing.get("sl_ladder_stage")
    action_sent  = existing.get("action_alerts_sent", 0)
    action_acked = existing.get("action_alerts_acked", 0)

    exit_price = _get_kite_exit_price(kite, tradingsymbol)
    if exit_price is None and last_pos_snapshot is not None:
        fallback = last_pos_snapshot.get("sell_price") or last_pos_snapshot.get("average_price")
        exit_price = float(fallback) if fallback else None
    if exit_price is None:
        exit_price = entry
        print(f"[position_tracker] {tradingsymbol}: could not determine exit price — falling back to entry")

    pnl_per_unit = exit_price - entry
    pnl = pnl_per_unit * qty
    if last_pos_snapshot is not None:
        realised = float(last_pos_snapshot.get("realised") or 0.0)
        if realised != 0.0:
            pnl = realised

    T = existing.get("target_t")
    r_multiple = (pnl_per_unit / T) if T else None
    compliance_ratio = (action_acked / action_sent) if action_sent > 0 else 1.0

    ladder_driven = _is_ladder_driven(exit_price, sl_ladder_stage)
    exit_type = "Ladder SL" if ladder_driven else "Manual / untracked flatten"

    market_note = (
        f"Closed at {datetime.now(IST).strftime('%H:%M IST')} · "
        f"entry={entry:.2f} exit={exit_price:.2f}"
    )

    trade_notifier.send_exit_summary(
        instrument=instrument,
        direction=direction,
        entry=entry,
        exit_price=exit_price,
        pnl=pnl,
        r_multiple=r_multiple,
        compliance_ratio=compliance_ratio,
        market_note=market_note,
        exit_type=exit_type,
    )

    _delete_position(tradingsymbol)
    print(f"[position_tracker] {tradingsymbol}: exit detected ({exit_type}) — tracking removed")


def _handle_disappeared(kite, tradingsymbol: str, last_pos_snapshot: dict | None) -> None:
    """A previously tracked tradingsymbol is no longer in the open position set."""
    existing = _load_position(tradingsymbol)
    if not existing:
        _remove_from_index(tradingsymbol)
        return

    if existing.get("confirm_count", 1) < 2:
        # Never confirmed — transient Kite artifact, not a real fill. Discard silently.
        print(f"[position_tracker] {tradingsymbol}: pending sighting vanished before "
              f"confirm_count reached 2 — discarding silently, no alert")
        _delete_position(tradingsymbol)
        return

    _finalize_exit(kite, tradingsymbol, existing, last_pos_snapshot)


# ──────────────────────────────────────────────────────────────
# Heartbeat (runs every minute)
# ──────────────────────────────────────────────────────────────

def run_heartbeat() -> None:
    """Pull open positions from Kite, discover/confirm/track/exit per tradingsymbol."""
    try:
        from src import kite_client
        kite = kite_client.get_kite()
        positions_data = kite.positions()
    except Exception as e:
        print(f"[position_tracker] heartbeat: cannot get Kite client/positions: {e}")
        return

    all_positions = positions_data.get("net", []) or []
    pos_by_symbol = {p["tradingsymbol"]: p for p in all_positions}
    open_tracked = {
        ts: p for ts, p in pos_by_symbol.items()
        if p.get("quantity", 0) != 0 and _underlying_from_tradingsymbol(ts) is not None
    }

    tracked_symbols = _load_index()
    if tracked_symbols or open_tracked:
        print(f"[position_tracker] heartbeat: tracked={tracked_symbols} open_tracked={list(open_tracked)}")

    # Exit / disappearance handling first, so a freshly-vacated key doesn't
    # get re-discovered as "new" in the same cycle.
    for ts in tracked_symbols:
        if ts not in open_tracked:
            _handle_disappeared(kite, ts, pos_by_symbol.get(ts))

    today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)

    for ts, pos in open_tracked.items():
        existing = _load_position(ts)
        if existing is None:
            _handle_new_sighting(ts, pos)
        elif existing.get("confirm_count", 1) < 2:
            _handle_confirm(kite, ts, pos, existing)
        else:
            _handle_ongoing(kite, ts, pos, existing, today_open)


# ──────────────────────────────────────────────────────────────
# Main entrypoint
# ──────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[position_tracker] Run at {datetime.now(IST).isoformat()}")
    run_heartbeat()


if __name__ == "__main__":
    main()
