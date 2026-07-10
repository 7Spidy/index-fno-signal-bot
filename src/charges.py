"""
Approximate Zerodha charges for NSE/BSE F&O options — used by paper_engine.py
for net P&L simulation.

IMPORTANT — ALL RATES IN THIS FILE ARE APPROXIMATE AND MUST BE VERIFIED against
Zerodha's current charges calculator at https://zerodha.com/charges/ before these
numbers are treated as exact for any live-trading reconciliation or audit. NSE/SEBI
rates change periodically; update the constants below after each revision. This
module flags its own approximation intentionally so that downstream callers are
never misled into treating paper P&L as exact.

Per-instrument lot sizes used by paper_engine.py must also be verified quarterly
against NSE's published lot-size schedule (SEBI revises them periodically).
"""

# ── Brokerage ────────────────────────────────────────────────────────────────
# Zerodha charges ₹20 flat per executed order leg (or 0.03% of turnover,
# whichever is lower). For options at typical ATM premiums, the flat ₹20
# dominates. Using flat for simplicity.
BROKERAGE_PER_LEG: float = 20.0   # ₹ per leg

# ── STT / CTT ────────────────────────────────────────────────────────────────
# Options (NSE/BSE): 0.1% of premium on the SELL side only.
STT_SELL_PCT: float = 0.001        # 0.1%

# ── Exchange transaction charges ─────────────────────────────────────────────
# NSE F&O: 0.053% of premium turnover, both legs (buy + sell).
# BSE F&O (SENSEX): 0.05% of premium turnover, both legs. Using NSE rate as
# a conservative approximation for all instruments.
NSE_EXCHANGE_PCT: float = 0.000530  # 0.053%

# ── SEBI charges ─────────────────────────────────────────────────────────────
# ₹10 per crore of turnover = 0.0001% of turnover, both legs.
SEBI_PCT: float = 0.000001          # 0.0001%

# ── Stamp duty ───────────────────────────────────────────────────────────────
# 0.003% of premium on the BUY side only.
STAMP_DUTY_BUY_PCT: float = 0.00003  # 0.003%

# ── GST ──────────────────────────────────────────────────────────────────────
# 18% on (brokerage + exchange charges + SEBI charges). NOT on STT or stamp duty.
GST_PCT: float = 0.18


def net_pnl(entry: float, exit_price: float, lot_size: int, direction: str) -> float:
    """Compute net P&L after approximate Zerodha charges.

    entry, exit_price: option premium per unit (in ₹)
    lot_size: number of units per lot (1 lot traded)
    direction: "CE" or "PE" — used only for validation, NOT for sign or
        buy/sell-side logic. Every position is a long option premium, so
        P&L is always (exit_price - entry) * lot_size regardless of
        direction; direction only ever affects which underlying move
        makes the premium itself rise (handled upstream in
        position_tracker.py, not here).

    Returns net P&L in ₹ (positive = profit, negative = loss).
    All charge computations use the premium (option price), not the underlying.
    """
    direction = direction.upper()
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")

    # Every position is a long option premium regardless of CE/PE — you
    # always buy at entry_price and sell at exit_price. Direction affects
    # only which underlying move makes the premium rise; it never flips
    # which side is buy vs sell, or the sign of gross P&L.
    buy_price  = entry
    sell_price = exit_price

    buy_turnover  = buy_price  * lot_size
    sell_turnover = sell_price * lot_size

    gross_pnl = (exit_price - entry) * lot_size

    # Brokerage: ₹20 per leg × 2 legs
    brokerage = BROKERAGE_PER_LEG * 2

    # STT: on sell turnover only
    stt = STT_SELL_PCT * sell_turnover

    # Exchange charges: both legs
    exchange = NSE_EXCHANGE_PCT * (buy_turnover + sell_turnover)

    # SEBI: both legs
    sebi = SEBI_PCT * (buy_turnover + sell_turnover)

    # Stamp duty: buy side only
    stamp = STAMP_DUTY_BUY_PCT * buy_turnover

    # GST: on brokerage + exchange + sebi (NOT on stt or stamp)
    gst = GST_PCT * (brokerage + exchange + sebi)

    total_charges = brokerage + stt + exchange + sebi + stamp + gst

    return gross_pnl - total_charges
