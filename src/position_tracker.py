"""
Trailing SL position tracker — advisory only, never places/modifies orders.

Triggered every minute by cron-job.org → workflow_dispatch on trade-tracker.yml.

Flow:
  1. Poll Discord for /enter or /exit messages since last seen.
  2. Dispatch to enter/exit handlers for any commands found.
  3. Always run heartbeat: iterate tracked positions, compute trailing SL,
     and post FYI or ACTION embeds to Discord.
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src import state
from src import discord_listener
from src import trade_notifier

IST = ZoneInfo("Asia/Kolkata")

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


def _rsi_favoring(direction: str, rsi_values: list) -> bool:
    """Return True if the RSI 3-point staircase is moving in the trade's favor."""
    r0, r1, r2 = rsi_values[0], rsi_values[1], rsi_values[2]
    if direction.upper() == "CE":
        return r0 < r1 and r1 < r2
    return r0 > r1 and r1 > r2


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


def compute_final_sl(
    ladder_sl: float,
    ai_sl: float,
    direction: str,
    sl_history: "list[float] | None" = None,
) -> float:
    """Combine ladder SL and AI-adjusted SL, always taking the tighter side,
    AND never regressing below any previously-returned final_sl for this
    position — this is the critical invariant that protects against T falling
    back down and recomputing a weaker ladder_sl. sl_history defaults to []
    for backward compatibility with any direct unit-test calls that predate
    this change."""
    direction = direction.upper()
    candidates = [ladder_sl, ai_sl] + list(sl_history or [])
    if direction == "CE":
        return max(candidates)
    return min(candidates)


def compute_ai_adjusted_target(
    direction: str,
    market_snapshot: dict,
    current_T: float,
    original_T: float,
    progress: float,
) -> float:
    """Bi-directional T revision. Only evaluated when progress >= 0.9 (caller's
    responsibility to gate this).

    Upward revision — requires 2 of these 3:
      (a) RSI 3-point staircase still rising in trade's favor
      (b) Dominant DI rising and > threshold (25 for indices, 24 for stocks)
      (c) progress >= 0.9 (always True here — gated by caller)
    If 2-of-3 hold: return current_T * 1.15

    Downward revision — RSI staircase broken in OPPOSITE direction AND dominant
    DI has flipped to the other side:
      return max(current_T * 0.9, original_T)
      — NEVER below original_T

    Otherwise: return current_T unchanged.
    No upper cap — can compound upward across heartbeats as long as 2-of-3
    re-confirms each time.
    """
    direction = direction.upper()
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")

    if progress < 0.9:
        return current_T

    rsi_values   = market_snapshot.get("rsi_last3")
    dmi_snapshot = market_snapshot.get("dmi_last")

    if rsi_values is None or len(rsi_values) < 3 or dmi_snapshot is None:
        return current_T

    pdi_vals = dmi_snapshot.get("pdi", [])
    ndi_vals = dmi_snapshot.get("ndi", [])
    if len(pdi_vals) < 2 or len(ndi_vals) < 2:
        return current_T

    # DI threshold: 25 for indices, 24 for stocks
    from src import config as _cfg
    instrument = market_snapshot.get("instrument", "")
    index_names = {i["name"] for i in _cfg.INSTRUMENTS}
    if instrument.upper() in index_names:
        di_threshold = _cfg.as_dict()["DI_THRESHOLD"]
    else:
        try:
            from src import stock_config as _sc
            di_threshold = _sc.DI_THRESHOLD
        except Exception:
            di_threshold = 25

    # Dominant DI for this direction and its trend
    if direction == "CE":
        dom_di_now  = pdi_vals[-1]
        dom_di_prev = pdi_vals[-2]
    else:
        dom_di_now  = ndi_vals[-1]
        dom_di_prev = ndi_vals[-2]

    cond_a = _rsi_favoring(direction, rsi_values)
    cond_b = dom_di_now > dom_di_prev and dom_di_now > di_threshold
    # cond_c = progress >= 0.9 — always True here (gated above)

    if cond_a or cond_b:
        return current_T * 1.15

    # Downward revision: RSI reversing AND dominant DI has flipped sides
    if direction == "CE":
        di_flipped = ndi_vals[-1] > pdi_vals[-1]
    else:
        di_flipped = pdi_vals[-1] > ndi_vals[-1]

    if _rsi_reversing(direction, rsi_values) and di_flipped:
        return max(current_T * 0.9, original_T)

    return current_T


# ──────────────────────────────────────────────────────────────
# Redis key helpers
# ──────────────────────────────────────────────────────────────

def _track_key(instrument: str) -> str:
    return f"track:{instrument}:{date.today().isoformat()}"


def _load_track(instrument: str) -> dict | None:
    raw = state.redis_get(_track_key(instrument))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_track(instrument: str, data: dict) -> None:
    state.redis_set(_track_key(instrument), json.dumps(data), ex=86400)


def _all_tracked_instruments() -> list[str]:
    """Return instrument names that have a track:*:today key in Redis.

    Upstash REST does not natively support KEYS or SCAN in the free-tier
    REST endpoint we use, so we probe all known instruments instead of
    trying a pattern scan.
    """
    from src import config
    tracked = []
    today = date.today().isoformat()
    known_names = [i["name"] for i in config.INSTRUMENTS]
    # Also include any stock instruments from stock_config if available
    try:
        from src import stock_config
        known_names += [i["symbol"] for i in stock_config.STOCKS]
    except Exception:
        pass
    for name in known_names:
        key = f"track:{name}:{today}"
        if state.redis_exists(key):
            tracked.append(name)
    return tracked


# ──────────────────────────────────────────────────────────────
# Instrument name extraction from tradingsymbol
# ──────────────────────────────────────────────────────────────

_KNOWN_UNDERLYINGS = [
    "BANKNIFTY", "MIDCPNIFTY", "FINNIFTY", "NIFTY", "SENSEX",
]


def _underlying_from_tradingsymbol(tradingsymbol: str) -> str | None:
    """Extract underlying name from a Kite tradingsymbol.

    Examples: NIFTY26JUN24600CE → NIFTY, BANKNIFTY26JUN52500PE → BANKNIFTY
    """
    sym = tradingsymbol.upper()
    for name in _KNOWN_UNDERLYINGS:
        if sym.startswith(name):
            return name
    return None


# ──────────────────────────────────────────────────────────────
# Intent payload lookup
# ──────────────────────────────────────────────────────────────

def _load_intent(instrument: str) -> dict | None:
    """Look up the most recent Redis intent payload for this instrument.

    Checks executor:pending_intent and executor:position (written by Repo 2).
    Returns the parsed dict if it matches, or None.
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
    return None


# ──────────────────────────────────────────────────────────────
# /enter handler
# ──────────────────────────────────────────────────────────────

def handle_enter() -> None:
    """Process /enter: snapshot open Kite positions and initialise Redis tracking."""
    print("[position_tracker] /enter detected — scanning open positions")
    try:
        from src import kite_client
        kite = kite_client.get_kite()
        positions_data = kite.positions()
    except Exception as e:
        print(f"[position_tracker] handle_enter: kite.positions() failed: {e}")
        trade_notifier.send_fyi(
            "Unknown", "?", 0.0, 0.0, 0.0
        )
        return

    all_positions = (
        positions_data.get("net", []) or []
    )
    open_positions = [p for p in all_positions if p.get("quantity", 0) != 0]

    if not open_positions:
        print("[position_tracker] handle_enter: no open positions found in Kite")
        return

    for pos in open_positions:
        tradingsymbol = pos.get("tradingsymbol", "")
        instrument = _underlying_from_tradingsymbol(tradingsymbol)
        if not instrument:
            print(f"[position_tracker] handle_enter: unrecognised symbol {tradingsymbol!r} — skip")
            continue

        # Idempotent: skip if already tracked
        existing = _load_track(instrument)
        if existing:
            print(f"[position_tracker] handle_enter: {instrument} already tracked — leave untouched")
            continue

        # Determine direction from tradingsymbol (CE/PE suffix)
        if tradingsymbol.upper().endswith("CE"):
            direction = "CE"
        elif tradingsymbol.upper().endswith("PE"):
            direction = "PE"
        else:
            print(f"[position_tracker] handle_enter: cannot determine direction for {tradingsymbol!r} — skip")
            continue

        entry = pos.get("average_price") or pos.get("buy_price") or 0.0

        # Look up original intent for T and original SL
        intent = _load_intent(instrument)
        T: float | None = None
        original_sl: float | None = None

        if intent:
            risk_pts  = intent.get("spot_risk_pts")
            target_rr = intent.get("target_rr")
            if risk_pts and target_rr:
                T = risk_pts * target_rr
            original_sl = intent.get("spot_sl")
            print(f"[position_tracker] handle_enter: {instrument} intent found — "
                  f"T={T}, original_sl={original_sl}")
        else:
            print(
                f"[position_tracker] handle_enter: no intent payload for {instrument} — "
                "T unavailable. Tracking limited to raw P&L."
            )
            trade_notifier.send_fyi(
                instrument, direction,
                ltp=entry,
                progress_pct=0.0,
                current_sl=entry,
            )

        prior_sl = original_sl if original_sl is not None else entry

        track_data = {
            "entry":          entry,
            "T":              T,
            "original_T":     T,
            "direction":      direction,
            "tradingsymbol":  tradingsymbol,
            "prior_sl":       prior_sl,
            "last_alert_sl":  prior_sl,
            "sl_history":     [prior_sl],
            "T_history":      [T] if T is not None else [],
            "opened_at":      datetime.now(IST).isoformat(),
            "action_alerts_sent":   0,
            "action_alerts_acked":  0,
            "instrument":     instrument,
        }
        _save_track(instrument, track_data)
        print(f"[position_tracker] handle_enter: tracking {instrument} {direction} "
              f"entry={entry} T={T} prior_sl={prior_sl}")


# ──────────────────────────────────────────────────────────────
# Heartbeat (runs every minute regardless of /enter or /exit)
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


def _get_rsi_snapshot(instrument: str, today_open: datetime) -> list[float] | None:
    """Fetch last 3 RSI values for the instrument's futures.

    Returns [r_oldest, r_middle, r_newest] or None on failure.
    """
    try:
        from src import kite_client, indicators, state as st
        raw = st.redis_get("kite:instrument_tokens")
        if not raw:
            return None
        tokens = json.loads(raw)
        token_info = tokens.get(instrument)
        if not token_info:
            return None
        df = kite_client.fetch_ohlcv(token_info["token"], today_open)
        rsi_series = indicators.rsi_wilder(df)
        last3 = rsi_series.dropna().iloc[-3:]
        if len(last3) < 3:
            return None
        return list(last3)
    except Exception as e:
        print(f"[position_tracker] _get_rsi_snapshot({instrument}): {e}")
        return None


def _get_dmi_snapshot(instrument: str, today_open: datetime) -> dict | None:
    """Fetch latest +DI, -DI, ADX for the instrument's futures.

    Returns {"pdi": [p0, p1, p2], "ndi": [n0, n1, n2], "adx": float} or None on failure.
    Mirrors the _get_rsi_snapshot pattern — looks up kite:instrument_tokens (index futures).
    """
    try:
        from src import kite_client, indicators, state as st
        raw = st.redis_get("kite:instrument_tokens")
        if not raw:
            return None
        tokens = json.loads(raw)
        token_info = tokens.get(instrument)
        if not token_info:
            return None
        df = kite_client.fetch_ohlcv(token_info["token"], today_open)
        pdi, ndi, adx = indicators.dmi_wilder(df)
        pdi_vals = pdi.dropna().iloc[-3:]
        ndi_vals = ndi.dropna().iloc[-3:]
        adx_vals = adx.dropna()
        if len(pdi_vals) < 3 or len(ndi_vals) < 3 or len(adx_vals) == 0:
            return None
        return {
            "pdi": list(pdi_vals),
            "ndi": list(ndi_vals),
            "adx": float(adx_vals.iloc[-1]),
        }
    except Exception as e:
        print(f"[position_tracker] _get_dmi_snapshot({instrument}): {e}")
        return None


def run_heartbeat() -> None:
    """Check all tracked positions and post FYI/ACTION Discord embeds."""
    tracked = _all_tracked_instruments()
    if not tracked:
        print("[position_tracker] heartbeat: no active positions — skipping")
        return

    print(f"[position_tracker] heartbeat: found {len(tracked)} tracked position(s): {tracked}")

    try:
        from src import kite_client
        kite = kite_client.get_kite()
    except Exception as e:
        print(f"[position_tracker] heartbeat: cannot get Kite client: {e}")
        return

    today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)

    for instrument in tracked:
        track = _load_track(instrument)
        if not track:
            continue

        entry         = track["entry"]
        T             = track.get("T")
        direction     = track["direction"]
        tradingsymbol = track["tradingsymbol"]
        prior_sl      = track["prior_sl"]
        last_alert_sl = track.get("last_alert_sl", prior_sl)

        # Fetch live LTP for the option contract
        try:
            ltp_data = kite.ltp([f"NFO:{tradingsymbol}"])
            ltp = float(ltp_data.get(f"NFO:{tradingsymbol}", {}).get("last_price", 0) or 0)
            if ltp == 0:
                print(f"[position_tracker] heartbeat: LTP=0 for {tradingsymbol} — skip")
                continue
        except Exception as e:
            print(f"[position_tracker] heartbeat: LTP fetch failed for {tradingsymbol}: {e}")
            continue

        # Progress and RSI snapshot (best-effort; non-fatal on failure)
        if T and T > 0:
            if direction == "CE":
                raw_progress = (ltp - entry) / T
            else:
                raw_progress = (entry - ltp) / T
            progress_pct = raw_progress * 100.0
        else:
            raw_progress = 0.0
            progress_pct = 0.0

        rsi3 = _get_rsi_snapshot(instrument, today_open)
        market_snapshot = {
            "rsi_last3":     rsi3,
            "progress":      raw_progress,
            "current_price": ltp,
            "T":             T,
            "instrument":    instrument,
        }

        sl_history = track.get("sl_history", [last_alert_sl])
        T_history  = track.get("T_history", [T] if T is not None else [])
        original_T = track.get("original_T", T)

        raw_progress_for_T_check = raw_progress  # already computed earlier in the loop, reuse

        if T and T > 0 and raw_progress_for_T_check >= 0.9:
            dmi3 = _get_dmi_snapshot(instrument, today_open)
            market_snapshot["dmi_last"] = dmi3
            new_T = compute_ai_adjusted_target(direction, market_snapshot, T, original_T, raw_progress_for_T_check)
            if new_T != T:
                T_history.append(new_T)
                if new_T > T:
                    trade_notifier.send_target_raised(instrument, direction, T, new_T, reason_summary="momentum confirmed (RSI+DMI)")
                else:
                    trade_notifier.send_target_trimmed(instrument, direction, T, new_T, reason_summary="momentum cooling")
                T = new_T
                track["T"] = T

        if T and T > 0:
            ladder_sl = compute_ladder_sl(entry, T, ltp, direction, last_alert_sl)
            ai_sl     = compute_ai_adjusted_sl(ladder_sl, direction, market_snapshot)
            final_sl  = compute_final_sl(ladder_sl, ai_sl, direction, sl_history)
        else:
            # T unknown — can't ladder; hold at last_alert_sl
            final_sl = last_alert_sl

        sl_history.append(final_sl)
        track["sl_history"] = sl_history
        track["T_history"]  = T_history
        track["last_alert_sl"] = final_sl

        # Check current Kite SL order
        kite_sl = _get_kite_sl_for(kite, tradingsymbol)

        action_needed = False
        if kite_sl is not None:
            if direction == "CE" and kite_sl < final_sl:
                action_needed = True
            elif direction == "PE" and kite_sl > final_sl:
                action_needed = True

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
                track["action_alerts_sent"] = track.get("action_alerts_sent", 0) + 1
        else:
            trade_notifier.send_fyi(
                instrument=instrument,
                direction=direction,
                ltp=ltp,
                progress_pct=progress_pct,
                current_sl=kite_sl if kite_sl is not None else final_sl,
            )
            # If there was a prior action alert, check if SL was moved up to match
            if track.get("action_alerts_sent", 0) > track.get("action_alerts_acked", 0):
                if kite_sl is not None:
                    if direction == "CE" and kite_sl >= final_sl:
                        track["action_alerts_acked"] = track.get("action_alerts_sent", 0)
                    elif direction == "PE" and kite_sl <= final_sl:
                        track["action_alerts_acked"] = track.get("action_alerts_sent", 0)

        # Update monotonicity floor
        track["prior_sl"] = final_sl
        _save_track(instrument, track)


# ──────────────────────────────────────────────────────────────
# /exit handler
# ──────────────────────────────────────────────────────────────

def handle_exit() -> None:
    """Process /exit: close out any positions no longer open in Kite."""
    print("[position_tracker] /exit detected — checking for closed positions")
    tracked = _all_tracked_instruments()
    if not tracked:
        print("[position_tracker] handle_exit: no tracked positions to close")
        return

    try:
        from src import kite_client
        kite = kite_client.get_kite()
        positions_data = kite.positions()
    except Exception as e:
        print(f"[position_tracker] handle_exit: kite.positions() failed: {e}")
        return

    all_positions = positions_data.get("net", []) or []
    open_syms = {
        p["tradingsymbol"]
        for p in all_positions
        if p.get("quantity", 0) != 0
    }

    for instrument in tracked:
        track = _load_track(instrument)
        if not track:
            continue

        tradingsymbol = track["tradingsymbol"]
        if tradingsymbol in open_syms:
            print(f"[position_tracker] handle_exit: {instrument} still open — skip")
            continue

        # Position closed — build exit summary
        entry     = track["entry"]
        direction = track["direction"]
        T         = track.get("T") or 0.0
        action_sent  = track.get("action_alerts_sent", 0)
        action_acked = track.get("action_alerts_acked", 0)

        # Find fill details
        exit_price = 0.0
        try:
            trades = kite.trades()
            for t in reversed(trades):
                if t.get("tradingsymbol") == tradingsymbol:
                    exit_price = float(t.get("average_price") or t.get("price") or 0)
                    break
        except Exception as e:
            print(f"[position_tracker] handle_exit: trades() failed: {e}")

        if direction == "CE":
            pnl = exit_price - entry
        else:
            pnl = entry - exit_price

        r_multiple = (pnl / T) if T > 0 else 0.0
        compliance_ratio = (action_acked / action_sent) if action_sent > 0 else 1.0

        # Quick market context note
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
        )

        state.redis_delete(_track_key(instrument))
        print(f"[position_tracker] handle_exit: {instrument} closed and tracking removed")

    # Update last_seen_msg_id after /exit processing handled in main()


# ──────────────────────────────────────────────────────────────
# Main entrypoint
# ──────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[position_tracker] Run at {datetime.now(IST).isoformat()}")

    channel_id = os.environ.get("TRADE_TRACKER_CHANNEL_ID", "")
    bot_token  = os.environ.get("DISCORD_BOT_TOKEN", "")

    if not channel_id or not bot_token:
        print("[position_tracker] TRADE_TRACKER_CHANNEL_ID or DISCORD_BOT_TOKEN not set — skipping listener")
        messages  = []
        commands  = []
    else:
        last_seen = state.redis_get("trade:last_seen_msg_id")
        messages  = discord_listener.fetch_new_messages(channel_id, bot_token, last_seen)
        commands  = discord_listener.extract_commands(messages)

    # Dispatch commands in order
    latest_msg_id: str | None = None
    for msg_id, cmd in commands:
        latest_msg_id = msg_id
        if cmd == "/enter":
            handle_enter()
        elif cmd == "/exit":
            handle_exit()

    # Persist the latest seen message ID (even if no command)
    if messages:
        latest_msg_id = messages[-1]["id"]
    if latest_msg_id:
        state.redis_set("trade:last_seen_msg_id", latest_msg_id)

    # Always run heartbeat — it exits immediately if no tracked positions
    run_heartbeat()


if __name__ == "__main__":
    main()
