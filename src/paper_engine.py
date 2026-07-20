"""
Paper trade engine for Phase 1 simulation.

Simulates entries and exits for all 17 instruments (3 indices + 14 stocks)
using ₹1,00,000 fixed daily capital. Never places real orders.

All state is stored in Redis under the paper: key namespace.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src import config, state, stock_config, trade_notifier
from src.charges import net_pnl

IST = ZoneInfo("Asia/Kolkata")

# ── Paper-trade constants ────────────────────────────────────────────────────
DAILY_CAPITAL: float = 50_000.0
DAILY_LOSS_PCT: float = 0.15
DAILY_LOSS_LIMIT: float = -(DAILY_CAPITAL * DAILY_LOSS_PCT)   # -15% of capital, computed

# Index lot sizes — verify quarterly against NSE published schedule.
# NIFTY: 75 (revised 2024), BANKNIFTY: 15 (revised 2024), SENSEX: 20.
INDEX_LOT_SIZES: dict[str, int] = {
    "NIFTY":     75,
    "BANKNIFTY": 15,
    "SENSEX":    20,
}

# EOD square-off: matches Repo 2's SQUAREOFF_IST exactly (both must agree)
_EOD_HOUR, _EOD_MINUTE = 15, 10

# Intent-consumed flag TTL: 2 hours (longer than intent's own 30-min TTL)
_CONSUMED_TTL = 7200

# Paper position index key
_PAPER_INDEX_KEY = "paper:position:index"


# ── Lot-size lookup ──────────────────────────────────────────────────────────

def _lot_size_for(instrument: str, asset_class: str) -> int | None:
    """Return lot size for instrument. None if unknown."""
    if asset_class == "INDEX":
        return INDEX_LOT_SIZES.get(instrument.upper())
    stock = stock_config.STOCK_BY_NAME.get(instrument.upper())
    if stock:
        return stock["lot_size"]
    from src import dynamic_stock_universe
    for p in dynamic_stock_universe.get_active_dynamic_stocks():
        if p["name"].upper() == instrument.upper():
            return p["lot_size"]
    return None


# ── Exchange segment for LTP lookup ─────────────────────────────────────────

def _exchange_for(instrument: str, asset_class: str) -> str:
    """Return exchange prefix for kite.ltp() — NFO or BFO."""
    if asset_class == "INDEX":
        return config.fno_exchange_for(instrument)
    return "NFO"   # all stock options are on NFO


# ── Redis key helpers ────────────────────────────────────────────────────────

def _paper_pos_key(tradingsymbol: str) -> str:
    return f"paper:position:{tradingsymbol}"


def _paper_capital_key(date_str: str) -> str:
    return f"paper:capital:{date_str}"


def _paper_pnl_key(date_str: str) -> str:
    return f"paper:daily_pnl:{date_str}"


def _paper_closed_key(date_str: str) -> str:
    return f"paper:closed:{date_str}"


def _paper_no_more_key(date_str: str) -> str:
    return f"paper:no_more_entries:{date_str}"


def _paper_eod_key(date_str: str) -> str:
    return f"paper:discord_eod_posted:{date_str}"


# ── Position index ───────────────────────────────────────────────────────────

def _load_paper_index() -> list[str]:
    raw = state.redis_get(_PAPER_INDEX_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_paper_index(symbols: list[str]) -> None:
    state.redis_set(_PAPER_INDEX_KEY, json.dumps(symbols), ex=86400)


def _add_to_paper_index(tradingsymbol: str) -> None:
    idx = _load_paper_index()
    if tradingsymbol not in idx:
        idx.append(tradingsymbol)
        _save_paper_index(idx)


def _remove_from_paper_index(tradingsymbol: str) -> None:
    idx = _load_paper_index()
    if tradingsymbol in idx:
        idx.remove(tradingsymbol)
        _save_paper_index(idx)


# ── Position CRUD ────────────────────────────────────────────────────────────

def load_paper_position(tradingsymbol: str) -> dict | None:
    raw = state.redis_get(_paper_pos_key(tradingsymbol))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def save_paper_position(tradingsymbol: str, data: dict) -> None:
    state.redis_set(_paper_pos_key(tradingsymbol), json.dumps(data), ex=86400)
    _add_to_paper_index(tradingsymbol)


def delete_paper_position(tradingsymbol: str) -> None:
    state.redis_delete(_paper_pos_key(tradingsymbol))
    _remove_from_paper_index(tradingsymbol)


def get_open_positions() -> list[dict]:
    """Return all currently-open paper positions."""
    symbols = _load_paper_index()
    positions = []
    for sym in symbols:
        pos = load_paper_position(sym)
        if pos:
            positions.append(pos)
    return positions


# ── Daily state helpers ──────────────────────────────────────────────────────

def get_or_init_daily_capital(date_str: str) -> float:
    """Always returns DAILY_CAPITAL — resets every day regardless of prior day P&L.

    Writes the key only if it doesn't exist yet (first run of the day).
    Never reads or carries forward the previous day's figure.
    """
    key = _paper_capital_key(date_str)
    existing = state.redis_get(key)
    if existing is None:
        state.redis_set(key, str(DAILY_CAPITAL), ex=86400)
        print(f"[paper_engine] Daily capital initialised to {DAILY_CAPITAL} for {date_str}")
    return DAILY_CAPITAL


def get_daily_pnl(date_str: str) -> float:
    raw = state.redis_get(_paper_pnl_key(date_str))
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except Exception:
        return 0.0


def _update_daily_pnl(date_str: str, delta: float) -> float:
    current = get_daily_pnl(date_str)
    new_val = current + delta
    state.redis_set(_paper_pnl_key(date_str), str(round(new_val, 2)), ex=86400)
    return new_val


def entries_blocked(date_str: str) -> bool:
    """True if the daily-loss breaker has tripped."""
    return state.redis_exists(_paper_no_more_key(date_str))


def _block_entries(date_str: str, reason: str) -> None:
    state.redis_set(_paper_no_more_key(date_str), reason, ex=86400)
    print(f"[paper_engine] Entries BLOCKED for {date_str}: {reason}")


def _block_reason(date_str: str) -> str | None:
    return state.redis_get(_paper_no_more_key(date_str))


def get_closed_positions(date_str: str) -> list[dict]:
    raw = state.redis_get(_paper_closed_key(date_str))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _append_closed_position(date_str: str, record: dict) -> None:
    closed = get_closed_positions(date_str)
    closed.append(record)
    state.redis_set(_paper_closed_key(date_str), json.dumps(closed), ex=86400)


# ── Capital availability check ───────────────────────────────────────────────

def _committed_premium(date_str: str) -> float:
    """Sum of entry_price * lot_size for all currently-open paper positions."""
    total = 0.0
    for pos in get_open_positions():
        if pos.get("date_str") == date_str:
            total += pos.get("entry_price", 0.0) * pos.get("lot_size", 0)
    return total


# ── Entry simulation ─────────────────────────────────────────────────────────

def simulate_entry(intent: dict, kite) -> None:
    """Simulate opening a paper trade from a signal intent.

    intent: parsed tracker:pending_intent payload
    kite: authenticated KiteConnect instance (read-only LTP fetch)

    Checks:
    1. entries_blocked gate
    2. daily capital covers this entry premium
    3. Fetches live option LTP as entry price
    4. Writes paper:position:{tradingsymbol}
    """
    date_str   = datetime.now(IST).strftime("%Y-%m-%d")
    instrument = intent.get("instrument", "").upper()
    direction  = intent.get("direction", "").upper()
    tradingsymbol = intent.get("tradingsymbol")
    asset_class   = intent.get("asset_class", "INDEX")
    spot_sl       = intent.get("spot_sl")         # absolute spot price
    target_pts    = intent.get("target_pts")       # spot points

    if not tradingsymbol:
        print(f"[paper_engine] {instrument}: no tradingsymbol in intent — skip entry")
        return

    # Gate: entries blocked for today
    get_or_init_daily_capital(date_str)
    if entries_blocked(date_str):
        reason = _block_reason(date_str) or "entries blocked"
        print(f"[paper_engine] {instrument}: entries blocked for {date_str} — skip")
        trade_notifier.send_trade_skipped(instrument, tradingsymbol, direction, reason)
        return

    # Already have an open paper position in this tradingsymbol? Skip duplicate
    if load_paper_position(tradingsymbol):
        print(f"[paper_engine] {tradingsymbol}: already has open paper position — skip")
        return

    lot_size = _lot_size_for(instrument, asset_class)
    if lot_size is None:
        print(f"[paper_engine] {instrument}: unknown lot size — skip entry")
        return

    # Fetch live option LTP as entry price
    exchange = _exchange_for(instrument, asset_class)
    ltp_key  = f"{exchange}:{tradingsymbol}"
    try:
        ltp_data = kite.ltp([ltp_key])
        entry_price = float(ltp_data.get(ltp_key, {}).get("last_price", 0) or 0)
    except Exception as e:
        print(f"[paper_engine] {tradingsymbol}: LTP fetch failed — {e} — skip entry")
        return

    if entry_price <= 0:
        print(f"[paper_engine] {tradingsymbol}: LTP={entry_price} invalid — skip entry")
        return

    # Capital check: remaining capital must cover this entry's full premium
    committed = _committed_premium(date_str)
    remaining = DAILY_CAPITAL - committed
    entry_cost = entry_price * lot_size
    if entry_cost > remaining:
        print(
            f"[paper_engine] {instrument}: capital exhausted "
            f"(need ₹{entry_cost:.0f}, have ₹{remaining:.0f}) — skip entry"
        )
        reason = f"Budget constraint: need ₹{entry_cost:.0f}, have ₹{remaining:.0f} remaining"
        trade_notifier.send_trade_skipped(instrument, tradingsymbol, direction, reason)
        return

    # Derive initial option SL from spot_sl via delta approximation
    delta = config.ATM_DELTA
    if spot_sl is not None and spot_sl > 0 and target_pts and target_pts > 0:
        spot_entry_approx = spot_sl   # spot_sl IS the spot SL price; need spot entry
        # spot_risk = distance from spot entry to spot SL; use target_pts / TARGET_RR
        spot_risk_pts = target_pts / config.TARGET_RR if config.TARGET_RR > 0 else target_pts
        option_risk_pts = spot_risk_pts * delta
        if direction == "CE":
            initial_sl_option = entry_price - option_risk_pts
        else:
            initial_sl_option = entry_price - option_risk_pts   # PE: SL below entry premium too
        initial_sl_option = max(initial_sl_option, 0.05)
    else:
        # Fallback: 30% of entry premium as initial SL buffer
        initial_sl_option = entry_price * 0.70
        print(f"[paper_engine] {tradingsymbol}: spot_sl missing — using 70% of entry as SL floor")

    # Convert target_pts (spot points) to option premium target
    option_target_pts = (target_pts * delta) if target_pts else None

    position = {
        "tradingsymbol":    tradingsymbol,
        "instrument":       instrument,
        "asset_class":      asset_class,
        "direction":        direction,
        "entry_price":      round(entry_price, 2),
        "sl_ladder_stage":  round(initial_sl_option, 2),
        "initial_sl":       round(initial_sl_option, 2),
        "target_t":         round(option_target_pts, 2) if option_target_pts else None,
        "lot_size":         lot_size,
        "date_str":         date_str,
        "entered_at":       datetime.now(IST).isoformat(),
        "current_ltp":      round(entry_price, 2),
    }
    save_paper_position(tradingsymbol, position)
    print(
        f"[paper_engine] ENTRY: {tradingsymbol} {direction} "
        f"entry={entry_price:.2f} sl={initial_sl_option:.2f} "
        f"lot={lot_size} cost=₹{entry_cost:.0f}"
    )


# ── Exit simulation ──────────────────────────────────────────────────────────

def simulate_exit(tradingsymbol: str, exit_price: float, reason: str) -> None:
    """Simulate closing a paper trade.

    Computes net P&L via charges.net_pnl, appends to paper:closed:{date},
    updates paper:daily_pnl:{date}, and evaluates circuit-breaker flags.
    """
    pos = load_paper_position(tradingsymbol)
    if not pos:
        print(f"[paper_engine] simulate_exit: no open position for {tradingsymbol}")
        return

    date_str    = pos.get("date_str", datetime.now(IST).strftime("%Y-%m-%d"))
    entry_price = pos["entry_price"]
    lot_size    = pos["lot_size"]
    direction   = pos["direction"]
    instrument  = pos["instrument"]

    pnl = net_pnl(entry_price, exit_price, lot_size, direction)

    record = {
        "tradingsymbol": tradingsymbol,
        "instrument":    instrument,
        "direction":     direction,
        "entry_price":   round(entry_price, 2),
        "exit_price":    round(exit_price, 2),
        "lot_size":      lot_size,
        "pnl_net":       round(pnl, 2),
        "reason":        reason,
        "entered_at":    pos.get("entered_at"),
        "exited_at":     datetime.now(IST).isoformat(),
    }
    _append_closed_position(date_str, record)

    new_pnl = _update_daily_pnl(date_str, pnl)
    delete_paper_position(tradingsymbol)

    print(
        f"[paper_engine] EXIT: {tradingsymbol} {direction} "
        f"entry={entry_price:.2f} exit={exit_price:.2f} "
        f"pnl=₹{pnl:.2f} daily_pnl=₹{new_pnl:.2f} reason={reason}"
    )

    # Daily-loss circuit breaker
    if new_pnl <= DAILY_LOSS_LIMIT and not entries_blocked(date_str):
        _block_entries(date_str, f"daily_loss_breaker: pnl={new_pnl:.2f}")


# ── EOD guard ────────────────────────────────────────────────────────────────

def eod_posted(date_str: str) -> bool:
    return state.redis_exists(_paper_eod_key(date_str))


def mark_eod_posted(date_str: str) -> None:
    state.redis_set(_paper_eod_key(date_str), "1", ex=86400)


def is_eod(now: datetime | None = None) -> bool:
    """True if current IST time is at or past 15:10."""
    now = now or datetime.now(IST)
    return (now.hour, now.minute) >= (_EOD_HOUR, _EOD_MINUTE)
