"""Tests for C2: live VWAP position + P0 low/high touched VWAP."""
import numpy as np
import pandas as pd

from src import signals

N = 30
VWAP_LEVEL = 23300.0


def _make_df(n=N, close_val=23350.0, low_val=None, high_val=None):
    lows  = np.full(n, close_val - 10) if low_val  is None else np.full(n, low_val)
    highs = np.full(n, close_val + 10) if high_val is None else np.full(n, high_val)
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  np.full(n, close_val - 5),
        "high":  highs,
        "low":   lows,
        "close": np.full(n, close_val),
        "volume": np.ones(n) * 1000,
    })


def _warmup(n, *tail):
    arr = np.full(n, 20.0)
    for i, v in enumerate(tail):
        arr[n - len(tail) + i] = v
    return pd.Series(arr)


def _cfg():
    return {
        "DI_THRESHOLD": 25,
        "REQUIRE_DI_DOMINANCE": True,
        "DI_TREND_CHECK": False,
        "strike_step": 50,
    }


def _eval(df, live_ltp, live_vwap, vwap_series=None):
    if vwap_series is None:
        vwap_series = pd.Series(np.full(N, VWAP_LEVEL))
    rsi = _warmup(N, 50.0, 55.0, 58.0)
    pdi = _warmup(N, 23.0, 26.0, 28.0)
    ndi = _warmup(N, 14.0, 13.0, 12.0)
    return signals.evaluate(
        df, vwap_series, rsi, pdi, ndi, _cfg(),
        live_ltp=live_ltp,
        live_vwap=live_vwap,
        live_rsi=58.0,
        live_pdi=28.0,
        live_ndi=12.0,
    )


# ── CE: live above live VWAP, P0 low dipped to or below VWAP ────────────────

def test_c2_ce_passes_when_live_above_vwap_and_p0_low_touched_vwap():
    # P0 low=23290 <= v0=23300 ✓, live_ltp=23360 > live_vwap=23320 ✓
    df = _make_df(close_val=23350.0, low_val=23290.0, high_val=23360.0)
    assert _eval(df, live_ltp=23360.0, live_vwap=23320.0)["ce"]["c2"] is True


def test_c2_ce_fails_when_p0_low_never_touched_vwap():
    # P0 low=23320 > v0=23300 → no touch → fail
    df = _make_df(close_val=23350.0, low_val=23320.0, high_val=23360.0)
    assert _eval(df, live_ltp=23360.0, live_vwap=23320.0)["ce"]["c2"] is False


def test_c2_ce_boundary_p0_low_exactly_on_vwap():
    # P0 low=23300 = v0=23300 → ≤ passes (boundary must pass)
    df = _make_df(close_val=23350.0, low_val=23300.0, high_val=23360.0)
    assert _eval(df, live_ltp=23360.0, live_vwap=23320.0)["ce"]["c2"] is True


def test_c2_ce_fails_when_live_below_live_vwap():
    # live_ltp < live_vwap → CE side fails regardless of P0 low
    df = _make_df(close_val=23350.0, low_val=23290.0, high_val=23360.0)
    assert _eval(df, live_ltp=23280.0, live_vwap=23320.0)["ce"]["c2"] is False


# ── PE: live below live VWAP, P0 high spiked to or above VWAP ───────────────

def test_c2_pe_passes_when_live_below_vwap_and_p0_high_touched_vwap():
    # P0 high=23310 >= v0=23300 ✓, live_ltp=23240 < live_vwap=23280 ✓
    df = _make_df(close_val=23250.0, low_val=23240.0, high_val=23310.0)
    assert _eval(df, live_ltp=23240.0, live_vwap=23280.0)["pe"]["c2"] is True


def test_c2_pe_fails_when_p0_high_never_touched_vwap():
    # P0 high=23290 < v0=23300 → no touch → fail
    df = _make_df(close_val=23250.0, low_val=23240.0, high_val=23290.0)
    assert _eval(df, live_ltp=23240.0, live_vwap=23280.0)["pe"]["c2"] is False


def test_c2_pe_boundary_p0_high_exactly_on_vwap():
    # P0 high=23300 = v0=23300 → ≥ passes (boundary must pass)
    df = _make_df(close_val=23250.0, low_val=23240.0, high_val=23300.0)
    assert _eval(df, live_ltp=23240.0, live_vwap=23280.0)["pe"]["c2"] is True


def test_c2_pe_fails_when_live_above_live_vwap():
    # live_ltp > live_vwap → PE side fails regardless of P0 high
    df = _make_df(close_val=23250.0, low_val=23240.0, high_val=23310.0)
    assert _eval(df, live_ltp=23310.0, live_vwap=23280.0)["pe"]["c2"] is False


# ── C2 tolerance: near-miss touches within C2_VWAP_TOUCH_TOLERANCE_PCT ──────
# v0=23300 → default 0.03% tolerance ≈ 6.99 points, so v0+tol ≈ 23306.99 (CE)
# and v0-tol ≈ 23293.01 (PE).

def test_c2_ce_passes_when_p0_low_misses_vwap_by_less_than_tolerance():
    # P0 low=23305 > v0=23300 (a "clean breakout", no exact touch) but within
    # the ~6.99pt tolerance band → still passes. Mirrors the real NIFTY
    # 10:45 case (candle_low missed vwap by 0.6pts / ~0.0025%).
    df = _make_df(close_val=23350.0, low_val=23305.0, high_val=23360.0)
    assert _eval(df, live_ltp=23360.0, live_vwap=23320.0)["ce"]["c2"] is True


def test_c2_ce_fails_when_p0_low_misses_vwap_by_more_than_tolerance():
    # P0 low=23310 misses v0=23300 by 10pts — outside the ~6.99pt tolerance
    # band → still correctly fails.
    df = _make_df(close_val=23350.0, low_val=23310.0, high_val=23360.0)
    assert _eval(df, live_ltp=23360.0, live_vwap=23320.0)["ce"]["c2"] is False


def test_c2_pe_passes_when_p0_high_misses_vwap_by_less_than_tolerance():
    # P0 high=23295 < v0=23300 (no exact touch) but within the ~6.99pt
    # tolerance band → still passes.
    df = _make_df(close_val=23250.0, low_val=23240.0, high_val=23295.0)
    assert _eval(df, live_ltp=23240.0, live_vwap=23280.0)["pe"]["c2"] is True


def test_c2_pe_fails_when_p0_high_misses_vwap_by_more_than_tolerance():
    # P0 high=23290 misses v0=23300 by 10pts — outside the ~6.99pt tolerance
    # band → still correctly fails.
    df = _make_df(close_val=23250.0, low_val=23240.0, high_val=23290.0)
    assert _eval(df, live_ltp=23240.0, live_vwap=23280.0)["pe"]["c2"] is False
