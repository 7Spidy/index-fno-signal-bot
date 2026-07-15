"""Tests for local_backtest/c5_liquidity_sweep.py — standalone C5 "Liquidity
Sweep" prototype. Per test_c5_liquidity_sweep_PLAN.md: unit tests only here.
Backtest validation run (100+ signals/instrument) and promotion gate are
tracked separately and are NOT part of this file."""
from datetime import datetime

import pytest

from local_backtest import c5_liquidity_sweep as c5
from local_backtest.c5_liquidity_sweep import Candle, DailyLevels, SweepSignal

DAY = "2024-06-10"


def _ts(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY} {hhmm}:00")


def _candle(hhmm, o, h, l, c) -> Candle:
    return Candle(ts=_ts(hhmm), open=o, high=h, low=l, close=c)


# ---------------------------------------------------------------------------
# compute_prior_day_levels
# ---------------------------------------------------------------------------

def test_prior_day_levels_max_high_min_low():
    candles = [
        _candle("09:15", 100, 110, 95, 105),
        _candle("09:20", 105, 120, 100, 115),
        _candle("09:25", 115, 118, 90, 110),
    ]
    levels = c5.compute_prior_day_levels(candles)
    assert levels.pdh == 120
    assert levels.pdl == 90


def test_prior_day_levels_single_candle():
    candles = [_candle("09:15", 100, 25100, 24950, 105)]
    levels = c5.compute_prior_day_levels(candles)
    assert levels.pdh == 25100
    assert levels.pdl == 24950


# ---------------------------------------------------------------------------
# compute_opening_range
# ---------------------------------------------------------------------------

def test_opening_range_within_window():
    candles = [
        _candle("09:15", 100, 110, 95, 105),
        _candle("09:20", 105, 130, 90, 115),
        _candle("09:25", 115, 120, 100, 110),
    ]
    levels = DailyLevels(pdh=200, pdl=50)
    levels = c5.compute_opening_range(levels, candles)
    assert levels.orh == 130
    assert levels.orl == 90


def test_opening_range_no_candles_in_range():
    # Simulated holiday-gap open: first candle of the day is at 10:00.
    candles = [
        _candle("10:00", 100, 110, 95, 105),
        _candle("10:05", 105, 120, 100, 115),
    ]
    levels = DailyLevels(pdh=200, pdl=50)
    levels = c5.compute_opening_range(levels, candles)
    assert levels.orh is None
    assert levels.orl is None


def test_opening_range_boundary_exclusion():
    candles = [
        _candle("09:14", 100, 9999, -9999, 105),   # excluded: before window
        _candle("09:15", 105, 130, 90, 110),        # included
        _candle("09:29", 110, 120, 95, 108),        # included
        _candle("09:30", 108, 8888, -8888, 112),    # excluded: window end is exclusive
    ]
    levels = DailyLevels(pdh=200, pdl=50)
    levels = c5.compute_opening_range(levels, candles)
    assert levels.orh == 130
    assert levels.orl == 90


# ---------------------------------------------------------------------------
# detect_sweep — bearish sweep (long setup), PDL = 25000
# ---------------------------------------------------------------------------

PDL = 25000.0
PDH = 25200.0


def _levels():
    return DailyLevels(pdh=PDH, pdl=PDL)


def test_long_sweep_wick_exactly_buffer_does_not_trigger():
    # low == pdl - buffer_pts (5) -> needs to be strictly less, not equal.
    sweep = _candle("10:00", 25010.0, 25015.0, PDL - 5, 25005.0)
    confirm = _candle("10:05", 25005.0, 25020.0, 25000.0, 25012.0)
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is None


def test_long_sweep_close_does_not_reclaim_pdl_does_not_trigger():
    # low is buffer+1 below PDL, but close is still below PDL.
    sweep = _candle("10:00", 24998.0, 25002.0, PDL - 6, 24990.0)
    confirm = _candle("10:05", 24990.0, 25000.0, 24985.0, 24995.0)
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is None


def test_long_sweep_wick_depth_below_min_wick_does_not_trigger():
    # buffer_pts=5, min_wick_pts=10 (custom): wick just past buffer but short of min_wick.
    with pytest_monkeypatch_instrument("TESTWICK", buffer_pts=5, min_wick_pts=10):
        sweep = _candle("10:00", 24998.0, 25002.0, PDL - 6, 25005.0)
        confirm = _candle("10:05", 25005.0, 25015.0, 25000.0, 25020.0)
        sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "TESTWICK")
        assert sig is None


def test_long_sweep_no_confirmation_does_not_trigger():
    # Wick and close conditions satisfied, wick_depth ok, but confirm fails to close > sweep.open.
    sweep = _candle("10:00", 24998.0, 25002.0, PDL - 10, 25005.0)
    confirm = _candle("10:05", 24998.0, 25000.0, 24990.0, 24998.0)  # confirm.close == sweep.open
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is None


def test_long_sweep_all_conditions_met_triggers():
    sweep = _candle("10:00", 24998.0, 25002.0, PDL - 10, 25005.0)
    confirm = _candle("10:05", 24998.0, 25015.0, 24995.0, 25010.0)  # confirm.close > sweep.open
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is not None
    assert sig.direction == "long"
    assert sig.sweep_extreme == PDL - 10
    assert sig.level_used == "PDL"
    assert sig.entry_ref_price == confirm.close


# ---------------------------------------------------------------------------
# detect_sweep — bullish sweep (short setup), PDH = 25200 — mirror of above
# ---------------------------------------------------------------------------

def test_short_sweep_wick_exactly_buffer_does_not_trigger():
    sweep = _candle("10:00", 25190.0, PDH + 5, 25185.0, 25195.0)
    confirm = _candle("10:05", 25195.0, 25200.0, 25180.0, 25188.0)
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is None


def test_short_sweep_close_does_not_reclaim_pdh_does_not_trigger():
    sweep = _candle("10:00", 25202.0, PDH + 6, 25198.0, 25210.0)  # close still above PDH
    confirm = _candle("10:05", 25210.0, 25215.0, 25200.0, 25205.0)
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is None


def test_short_sweep_wick_depth_below_min_wick_does_not_trigger():
    with pytest_monkeypatch_instrument("TESTWICK", buffer_pts=5, min_wick_pts=10):
        sweep = _candle("10:00", 25202.0, PDH + 6, 25198.0, 25190.0)
        confirm = _candle("10:05", 25190.0, 25195.0, 25180.0, 25175.0)
        sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "TESTWICK")
        assert sig is None


def test_short_sweep_no_confirmation_does_not_trigger():
    sweep = _candle("10:00", 25202.0, PDH + 10, 25198.0, 25190.0)
    confirm = _candle("10:05", 25202.0, 25205.0, 25195.0, 25202.0)  # confirm.close == sweep.open
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is None


def test_short_sweep_all_conditions_met_triggers():
    sweep = _candle("10:00", 25202.0, PDH + 10, 25198.0, 25190.0)
    confirm = _candle("10:05", 25202.0, 25203.0, 25190.0, 25195.0)  # confirm.close < sweep.open
    sig = c5.detect_sweep([sweep, confirm], 0, _levels(), "NIFTY")
    assert sig is not None
    assert sig.direction == "short"
    assert sig.sweep_extreme == PDH + 10
    assert sig.level_used == "PDH"
    assert sig.entry_ref_price == confirm.close


# ---------------------------------------------------------------------------
# detect_sweep — no confirmation candle available (idx is last candle)
# ---------------------------------------------------------------------------

def test_detect_sweep_returns_none_when_no_confirmation_candle_exists():
    candles = [
        _candle("10:00", 100, 110, 90, 105),
        _candle("10:05", 105, 115, 95, 110),
        _candle("10:10", 110, 120, 100, 115),
    ]
    sig = c5.detect_sweep(candles, len(candles) - 1, _levels(), "NIFTY")
    assert sig is None


# ---------------------------------------------------------------------------
# Per-instrument parameter isolation
# ---------------------------------------------------------------------------

def test_instrument_params_isolated_same_candles_different_outcomes():
    # 10 pts below PDL: triggers for NIFTY (buffer=5) but not BANKNIFTY (15) or SENSEX (50).
    sweep = _candle("10:00", 24998.0, 25002.0, PDL - 10, 25005.0)
    confirm = _candle("10:05", 24998.0, 25015.0, 24995.0, 25010.0)
    candles = [sweep, confirm]

    nifty_sig = c5.detect_sweep(candles, 0, _levels(), "NIFTY")
    banknifty_sig = c5.detect_sweep(candles, 0, _levels(), "BANKNIFTY")
    sensex_sig = c5.detect_sweep(candles, 0, _levels(), "SENSEX")

    assert nifty_sig is not None
    assert banknifty_sig is None
    assert sensex_sig is None


# ---------------------------------------------------------------------------
# compute_entry_sl_target
# ---------------------------------------------------------------------------

def test_entry_sl_target_long_side_ordering():
    signal = SweepSignal(
        instrument="NIFTY", direction="long", sweep_candle_idx=0, confirm_candle_idx=1,
        sweep_extreme=24990.0, entry_ref_price=25010.0, level_used="PDL", target_r_multiple=1.5,
    )
    result = c5.compute_entry_sl_target(signal, entry_buffer_pct=0.01)
    assert result["entry"] > signal.entry_ref_price
    assert result["sl"] < signal.sweep_extreme
    assert result["target"] > result["entry"]
    assert result["risk_pts"] > 0


def test_entry_sl_target_short_side_ordering():
    signal = SweepSignal(
        instrument="NIFTY", direction="short", sweep_candle_idx=0, confirm_candle_idx=1,
        sweep_extreme=25210.0, entry_ref_price=25190.0, level_used="PDH", target_r_multiple=1.5,
    )
    result = c5.compute_entry_sl_target(signal, entry_buffer_pct=0.01)
    assert result["entry"] < signal.entry_ref_price
    assert result["sl"] > signal.sweep_extreme
    assert result["target"] < result["entry"]
    assert result["risk_pts"] > 0


def test_entry_sl_target_r_multiple_exact_math():
    # NIFTY sl_buffer = buffer_pts * 0.5 = 2.5. With entry_buffer_pct=0, entry == entry_ref_price.
    signal = SweepSignal(
        instrument="NIFTY", direction="long", sweep_candle_idx=0, confirm_candle_idx=1,
        sweep_extreme=92.5, entry_ref_price=100.0, level_used="PDL", target_r_multiple=1.5,
    )
    result = c5.compute_entry_sl_target(signal, entry_buffer_pct=0.0)
    assert result["entry"] == 100.0
    assert result["sl"] == 90.0
    assert result["risk_pts"] == 10.0
    assert result["target"] == 115.0
    assert result["target"] - result["entry"] == 15.0


# ---------------------------------------------------------------------------
# scan_for_sweeps
# ---------------------------------------------------------------------------

def _flat_candle(hhmm, price=25100.0):
    return _candle(hhmm, price, price + 5, price - 5, price)


def _long_sweep_pair(hhmm_sweep, hhmm_confirm):
    sweep = _candle(hhmm_sweep, 24998.0, 25002.0, PDL - 10, 25005.0)
    confirm = _candle(hhmm_confirm, 24998.0, 25015.0, 24995.0, 25010.0)
    return sweep, confirm


def _short_sweep_pair(hhmm_sweep, hhmm_confirm):
    sweep = _candle(hhmm_sweep, 25202.0, PDH + 10, 25198.0, 25190.0)
    confirm = _candle(hhmm_confirm, 25202.0, 25203.0, 25190.0, 25195.0)
    return sweep, confirm


PRIOR_DAY = [_candle("09:15", 25050.0, PDH, PDL, 25100.0)]


def test_scan_returns_one_signal_when_exactly_one_pattern_injected():
    sweep, confirm = _long_sweep_pair("09:35", "09:40")
    candles = [
        _flat_candle("09:15"), _flat_candle("09:20"), _flat_candle("09:25"),
        _flat_candle("09:30"), sweep, confirm,
        _flat_candle("09:45"), _flat_candle("09:50"),
    ]
    sigs = c5.scan_for_sweeps(candles, PRIOR_DAY, "NIFTY")
    assert len(sigs) == 1
    assert sigs[0].direction == "long"
    assert sigs[0].sweep_candle_idx == 4


def test_scan_returns_empty_list_when_no_patterns():
    candles = [_flat_candle(hhmm) for hhmm in ("09:15", "09:20", "09:25", "09:30", "09:35")]
    sigs = c5.scan_for_sweeps(candles, PRIOR_DAY, "NIFTY")
    assert sigs == []


def test_scan_returns_two_nonoverlapping_signals_in_chronological_order():
    long_sweep, long_confirm = _long_sweep_pair("09:25", "09:30")
    short_sweep, short_confirm = _short_sweep_pair("09:50", "09:55")
    candles = [
        _flat_candle("09:15"), _flat_candle("09:20"),
        long_sweep, long_confirm,
        _flat_candle("09:35"), _flat_candle("09:40"), _flat_candle("09:45"),
        short_sweep, short_confirm,
    ]
    sigs = c5.scan_for_sweeps(candles, PRIOR_DAY, "NIFTY")
    assert len(sigs) == 2
    assert sigs[0].sweep_candle_idx < sigs[1].sweep_candle_idx
    assert sigs[0].direction == "long"
    assert sigs[1].direction == "short"


# ---------------------------------------------------------------------------
# Helper: temporarily register a custom instrument in INSTRUMENT_PARAMS.
# ---------------------------------------------------------------------------

class pytest_monkeypatch_instrument:
    """Context manager that adds (and then removes) a test-only entry in
    c5.INSTRUMENT_PARAMS, used to isolate buffer_pts vs min_wick_pts cases
    that the real NIFTY/BANKNIFTY/SENSEX params (which set them equal)
    can't exercise on their own."""

    def __init__(self, name, buffer_pts, min_wick_pts):
        self.name = name
        self.params = {"buffer_pts": buffer_pts, "min_wick_pts": min_wick_pts}

    def __enter__(self):
        c5.INSTRUMENT_PARAMS[self.name] = self.params
        return self

    def __exit__(self, *exc):
        del c5.INSTRUMENT_PARAMS[self.name]
