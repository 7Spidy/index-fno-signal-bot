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
# Pending-command state (deferred-ack retry for /enter and /exit)
# ──────────────────────────────────────────────────────────────

def _pending_key(kind: str) -> str:
    return f"pending:{kind}:{date.today().isoformat()}"


def _load_pending(kind: str) -> dict | None:
    raw = state.redis_get(_pending_key(kind))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_pending(kind: str, msg_id: str, attempts: int) -> None:
    state.redis_set(
        _pending_key(kind),
        json.dumps({
            "msg_id": msg_id,
            "attempts": attempts,
            "first_seen_at": datetime.now(IST).isoformat(),
        }),
        ex=300,
    )


def _clear_pending(kind: str) -> None:
    state.redis_delete(_pending_key(kind))


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

def _try_enter() -> bool:
    """Snapshot open Kite positions and initialise Redis tracking.

    Returns True if at least one open position was found, False otherwise.
    """
    print("[position_tracker] _try_enter: scanning open positions")
    try:
        from src import kite_client
        kite = kite_client.get_kite()
        positions_data = kite.positions()
    except Exception as e:
        print(f"[position_tracker] _try_enter: kite.positions() failed: {e}")
        trade_notifier.send_fyi(
            "Unknown", "?", 0.0, 0.0, 0.0
        )
        return False

    all_positions = (
        positions_data.get("net", []) or []
    )
    open_positions = [p for p in all_positions if p.get("quantity", 0) != 0]

    if not open_positions:
        print("[position_tracker] _try_enter: no open positions found in Kite")
        return False

    for pos in open_positions:
        tradingsymbol = pos.get("tradingsymbol", "")
        instrument = _underlying_from_tradingsymbol(tradingsymbol)
        if not instrument:
            print(f"[position_tracker] _try_enter: unrecognised symbol {tradingsymbol!r} — skip")
            continue

        # Idempotent: skip if already tracked
        existing = _load_track(instrument)
        if existing:
            print(f"[position_tracker] _try_enter: {instrument} already tracked — leave untouched")
            continue

        # Determine direction from tradingsymbol (CE/PE suffix)
        if tradingsymbol.upper().endswith("CE"):
            direction = "CE"
        elif tradingsymbol.upper().endswith("PE"):
            direction = "PE"
        else:
            print(f"[position_tracker] _try_enter: cannot determine direction for {tradingsymbol!r} — skip")
            continue

        entry = pos.get("average_price") or pos.get("buy_price") or 0.0

        # Look up original intent for T and original SL
        intent = _load_intent(instrument)
        T: float | None = None
        original_sl: float | None = None
        kite_sl_initial: float | None = None  # fetched only in no-intent path

        if intent:
            risk_pts  = intent.get("spot_risk_pts")
            target_rr = intent.get("target_rr")
            if risk_pts and target_rr:
                T = risk_pts * target_rr
            original_sl = intent.get("spot_sl")
            print(f"[position_tracker] _try_enter: {instrument} intent found — "
                  f"T={T}, original_sl={original_sl}")
        else:
            print(
                f"[position_tracker] _try_enter: no intent payload for {instrument} — "
                "T unavailable. Tracking limited to raw P&L."
            )
            kite_sl_initial = _get_kite_sl_for(kite, tradingsymbol)
            trade_notifier.send_fyi(
                instrument, direction,
                ltp=entry,
                progress_pct=0.0,
                current_sl=kite_sl_initial if kite_sl_initial is not None else entry,
            )

        prior_sl = (
            original_sl if original_sl is not None
            else kite_sl_initial if kite_sl_initial is not None
            else entry
        )

        track_data = {
            "entry":          entry,
            "T":              T,
            "direction":      direction,
            "tradingsymbol":  tradingsymbol,
            "prior_sl":       prior_sl,
            "last_alert_sl":  prior_sl,
            "opened_at":      datetime.now(IST).isoformat(),
            "action_alerts_sent":   0,
            "action_alerts_acked":  0,
            "instrument":     instrument,
        }
        _save_track(instrument, track_data)
        print(f"[position_tracker] _try_enter: tracking {instrument} {direction} "
              f"entry={entry} T={T} prior_sl={prior_sl}")

    return True


def handle_enter(msg_id: str) -> bool:
    """Retry-safe /enter handler.

    Returns True when the command is resolved (position found or all attempts
    exhausted). Returns False to signal the caller to not advance
    last_seen_msg_id — the same message should be re-delivered next cycle.
    """
    pending = _load_pending("enter")

    found = _try_enter()
    if found:
        _clear_pending("enter")
        return True

    attempts = (pending["attempts"] + 1) if pending else 1

    if attempts >= 3:
        print(f"[position_tracker] handle_enter: giving up after {attempts} attempts — no open position found")
        trade_notifier.send_enter_failed(attempts)
        _clear_pending("enter")
        return True  # resolved (given up) — allow message to be marked seen

    print(f"[position_tracker] handle_enter: attempt {attempts}/3 — no open position yet, will retry")
    _save_pending("enter", msg_id, attempts)
    return False  # not resolved — do not advance last_seen past this message


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

        if T and T > 0:
            ladder_sl = compute_ladder_sl(entry, T, ltp, direction, last_alert_sl)
            ai_sl     = compute_ai_adjusted_sl(ladder_sl, direction, market_snapshot)
            final_sl  = compute_final_sl(ladder_sl, ai_sl, direction)
        else:
            # T unknown — can't ladder; hold at last_alert_sl
            final_sl = last_alert_sl

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

def _try_exit() -> bool:
    """Check whether any tracked positions are now closed in Kite and clean them up.

    Returns True if at least one tracked position was found closed, False if all
    tracked positions are still open or Kite could not be reached.
    """
    tracked = _all_tracked_instruments()
    if not tracked:
        return True  # nothing to close — consider resolved

    try:
        from src import kite_client
        kite = kite_client.get_kite()
        positions_data = kite.positions()
    except Exception as e:
        print(f"[position_tracker] _try_exit: kite.positions() failed: {e}")
        return False

    all_positions = positions_data.get("net", []) or []
    open_syms = {
        p["tradingsymbol"]
        for p in all_positions
        if p.get("quantity", 0) != 0
    }

    found_closed = False
    for instrument in tracked:
        track = _load_track(instrument)
        if not track:
            continue

        tradingsymbol = track["tradingsymbol"]
        if tradingsymbol in open_syms:
            print(f"[position_tracker] _try_exit: {instrument} still open — skip")
            continue

        # Position closed — build exit summary
        found_closed = True
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
            print(f"[position_tracker] _try_exit: trades() failed: {e}")

        if direction == "CE":
            pnl = exit_price - entry
        else:
            pnl = entry - exit_price

        r_multiple = (pnl / T) if T > 0 else 0.0
        compliance_ratio = (action_acked / action_sent) if action_sent > 0 else 1.0

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
        print(f"[position_tracker] _try_exit: {instrument} closed and tracking removed")

    return found_closed


def handle_exit(msg_id: str) -> bool:
    """Retry-safe /exit handler.

    Returns True when the command is resolved (position closed, nothing to close,
    or all attempts exhausted). Returns False to signal the caller to not advance
    last_seen_msg_id — the same message should be re-delivered next cycle.
    """
    print("[position_tracker] /exit detected — checking for closed positions")

    # Fast path: no tracked positions — not a race condition, just a no-op
    if not _all_tracked_instruments():
        print("[position_tracker] handle_exit: no tracked positions to close")
        return True

    pending = _load_pending("exit")

    found = _try_exit()
    if found:
        _clear_pending("exit")
        return True

    attempts = (pending["attempts"] + 1) if pending else 1

    if attempts >= 3:
        print(f"[position_tracker] handle_exit: giving up after {attempts} attempts — position still shows open")
        trade_notifier.send_exit_failed(attempts)
        _clear_pending("exit")
        return True  # resolved (given up) — allow message to be marked seen

    print(f"[position_tracker] handle_exit: attempt {attempts}/3 — position still open in Kite, will retry")
    _save_pending("exit", msg_id, attempts)
    return False  # not resolved — do not advance last_seen past this message


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

    # Dispatch commands in order; stop at first unresolved command
    latest_resolved_msg_id: str | None = None
    unresolved = False

    for msg_id, cmd in commands:
        if cmd == "/enter":
            resolved = handle_enter(msg_id)
        elif cmd == "/exit":
            resolved = handle_exit(msg_id)
        else:
            resolved = True

        if resolved:
            latest_resolved_msg_id = msg_id
        else:
            unresolved = True
            break  # retry this command next cycle

    # Advance last_seen_msg_id only as far as resolved commands allow
    if not unresolved:
        # All commands resolved — advance to last fetched message (normal behaviour)
        latest_msg_id: str | None = None
        if messages:
            latest_msg_id = messages[-1]["id"]
        elif latest_resolved_msg_id:
            latest_msg_id = latest_resolved_msg_id
        if latest_msg_id:
            state.redis_set("trade:last_seen_msg_id", latest_msg_id)
    else:
        # Unresolved command — only advance up to the last message that did resolve
        if latest_resolved_msg_id:
            state.redis_set("trade:last_seen_msg_id", latest_resolved_msg_id)
        # else: nothing resolved — leave cursor unchanged so the command is re-delivered

    # Always run heartbeat — it exits immediately if no tracked positions
    run_heartbeat()


if __name__ == "__main__":
    main()
