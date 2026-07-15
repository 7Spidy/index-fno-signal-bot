"""
c5_liquidity_sweep.py

Standalone C5 "Liquidity Sweep" candidate signal for local_backtest/.
NOT wired into src/signals.py. Prototype only, per standing rule:
100+ signals required per instrument before treating results as reliable.

Applies to: NIFTY, BANKNIFTY, SENSEX (5-min candles).

This module is intentionally dependency-free from src/ so it can be
iterated on without touching production code. If promoted, port the
logic into src/signals.py as a new C5 function following the existing
C1-C4 pattern.
"""

from dataclasses import dataclass
from datetime import time
from typing import Optional, List, Literal

Direction = Literal["long", "short"]

# Instrument-specific tuning. Adjust after first backtest pass.
INSTRUMENT_PARAMS = {
    "NIFTY": {"buffer_pts": 5, "min_wick_pts": 5},
    "BANKNIFTY": {"buffer_pts": 15, "min_wick_pts": 15},
    "SENSEX": {"buffer_pts": 50, "min_wick_pts": 50},
}

OPENING_RANGE_START = time(9, 15)
OPENING_RANGE_END = time(9, 30)


@dataclass
class Candle:
    ts: object  # datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class DailyLevels:
    pdh: float  # prior day high
    pdl: float  # prior day low
    orh: Optional[float] = None  # opening range high (set after 09:30)
    orl: Optional[float] = None  # opening range low


@dataclass
class SweepSignal:
    instrument: str
    direction: Direction
    sweep_candle_idx: int
    confirm_candle_idx: int
    sweep_extreme: float       # low[i] for long, high[i] for short
    entry_ref_price: float     # LTP at confirm candle close, pre-buffer
    level_used: str            # "PDH" or "PDL"
    target_r_multiple: float


def compute_prior_day_levels(prior_day_candles: List[Candle]) -> DailyLevels:
    """Compute PDH/PDL from the prior trading day's full candle set."""
    highs = [c.high for c in prior_day_candles]
    lows = [c.low for c in prior_day_candles]
    return DailyLevels(pdh=max(highs), pdl=min(lows))


def compute_opening_range(levels: DailyLevels, today_candles: List[Candle]) -> DailyLevels:
    """Update levels with ORH/ORL once 09:15-09:30 candles are available."""
    or_candles = [c for c in today_candles if OPENING_RANGE_START <= c.ts.time() < OPENING_RANGE_END]
    if or_candles:
        levels.orh = max(c.high for c in or_candles)
        levels.orl = min(c.low for c in or_candles)
    return levels


def detect_sweep(
    candles: List[Candle],
    idx: int,
    levels: DailyLevels,
    instrument: str,
) -> Optional[SweepSignal]:
    """
    Evaluate candle[idx] as a potential sweep, candle[idx+1] as confirmation.
    Returns a SweepSignal if both conditions are met, else None.

    C5a - Sweep detection (candle i = idx):
      Bearish sweep (-> long setup):  low[i]  < PDL - buffer   AND close[i] > PDL
      Bullish sweep (-> short setup): high[i] > PDH + buffer   AND close[i] < PDH
      Wick depth beyond level must be >= min_wick_pts

    C5b - Confirmation (candle i+1):
      Long:  close[i+1] > open[i]
      Short: close[i+1] < open[i]
    """
    if idx + 1 >= len(candles):
        return None  # no confirmation candle available yet

    params = INSTRUMENT_PARAMS[instrument]
    buffer_pts = params["buffer_pts"]
    min_wick_pts = params["min_wick_pts"]

    sweep = candles[idx]
    confirm = candles[idx + 1]

    # Bearish sweep -> long setup
    if sweep.low < levels.pdl - buffer_pts and sweep.close > levels.pdl:
        wick_depth = levels.pdl - sweep.low
        if wick_depth >= min_wick_pts and confirm.close > sweep.open:
            return SweepSignal(
                instrument=instrument,
                direction="long",
                sweep_candle_idx=idx,
                confirm_candle_idx=idx + 1,
                sweep_extreme=sweep.low,
                entry_ref_price=confirm.close,
                level_used="PDL",
                target_r_multiple=1.5,
            )

    # Bullish sweep -> short setup
    if sweep.high > levels.pdh + buffer_pts and sweep.close < levels.pdh:
        wick_depth = sweep.high - levels.pdh
        if wick_depth >= min_wick_pts and confirm.close < sweep.open:
            return SweepSignal(
                instrument=instrument,
                direction="short",
                sweep_candle_idx=idx,
                confirm_candle_idx=idx + 1,
                sweep_extreme=sweep.high,
                entry_ref_price=confirm.close,
                level_used="PDH",
                target_r_multiple=1.5,
            )

    return None


def compute_entry_sl_target(signal: SweepSignal, entry_buffer_pct: float = 0.01):
    """
    Mirrors existing engine conventions:
      Entry: marketable LIMIT at ref price +/- 1% buffer
      SL: sweep extreme +/- small buffer (reuse instrument buffer_pts)
      Target: fixed R-multiple, used as ladder denominator T (SL-only exit)
    """
    params = INSTRUMENT_PARAMS[signal.instrument]
    sl_buffer = params["buffer_pts"] * 0.5  # tighter than sweep-detection buffer

    if signal.direction == "long":
        entry = signal.entry_ref_price * (1 + entry_buffer_pct)
        sl = signal.sweep_extreme - sl_buffer
        risk = entry - sl
        target = entry + risk * signal.target_r_multiple
    else:
        entry = signal.entry_ref_price * (1 - entry_buffer_pct)
        sl = signal.sweep_extreme + sl_buffer
        risk = sl - entry
        target = entry - risk * signal.target_r_multiple

    return {"entry": entry, "sl": sl, "target": target, "risk_pts": risk}


def scan_for_sweeps(
    candles: List[Candle],
    prior_day_candles: List[Candle],
    instrument: str,
) -> List[SweepSignal]:
    """
    Full scan entrypoint for the backtester.
    candles: today's 5-min candles (closed candles only, chronological)
    prior_day_candles: prior trading day's full 5-min candle set
    """
    levels = compute_prior_day_levels(prior_day_candles)
    levels = compute_opening_range(levels, candles)

    signals = []
    for i in range(len(candles) - 1):
        sig = detect_sweep(candles, i, levels, instrument)
        if sig:
            signals.append(sig)
    return signals
