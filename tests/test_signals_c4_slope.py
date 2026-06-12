"""Tests for C4 DI 3-candle slope requirement."""
import numpy as np
import pandas as pd
import pytest

from src import signals


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_df(n=30):
    """Minimal OHLCV DataFrame with n rows."""
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  np.full(n, 23000.0),
        "high":  np.full(n, 23010.0),
        "low":   np.full(n, 22990.0),
        "close": np.full(n, 23000.0),
        "volume": np.ones(n) * 1000,
    })

def _make_series(n, vals):
    """Series of length n, last len(vals) entries filled from vals list.

    Earlier entries get a flat low warm-up value (not NaN) so the
    >=15 non-null indicator guard in evaluate() is satisfied; the warm-up
    sits below every test value, so it never alters the slope outcome that
    the assertions care about.
    """
    WARMUP = 5.0
    arr = np.full(n, WARMUP)
    for i, v in enumerate(vals):
        arr[n - len(vals) + i] = v
    return pd.Series(arr)

def _cfg():
    return {
        "MOMENTUM_RULE":             "close_gt_prev_close",
        "RSI_SLOPE_LOOKBACK":        3,
        "VWAP_CROSS_WINDOW_CANDLES": 6,
        "DI_THRESHOLD":              25,
        "REQUIRE_DI_DOMINANCE":      True,
        "DI_TREND_CHECK":            True,
        "USE_ADX_FILTER":            False,
        "ADX_MIN":                   20,
        "COOLDOWN_CANDLES":          3,
        "SESSION_START_IST":         "09:15",
        "EVAL_WINDOW_IST":           ("09:40", "14:45"),
        "strike_step":               50,
    }

# idx0 = n-2 = 28, idx1 = 27, idx2 = 26
N = 30


# ── C4 CE (pdi slope) ────────────────────────────────────────────────────────

def test_c4_ce_passes_strict_3candle_rise():
    """pdi strictly rising over 3 candles → ce_c4 True."""
    df = _make_df(N)
    pdi = _make_series(N, [26.0, 28.0, 30.0])   # idx2=26, idx1=28, idx0=30
    ndi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [22990.0] * N)
    rsi  = _make_series(N, [55.0, 58.0, 61.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c4"] is True


def test_c4_ce_fails_flat_middle():
    """pdi flat between idx2 and idx1 → ce_c4 False (not strictly rising)."""
    df = _make_df(N)
    pdi = _make_series(N, [26.0, 26.0, 30.0])   # idx2==idx1 — not strict
    ndi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [22990.0] * N)
    rsi  = _make_series(N, [55.0, 58.0, 61.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c4"] is False


def test_c4_ce_fails_dip_in_middle():
    """pdi dips at idx1 then recovers → not a strict staircase → False."""
    df = _make_df(N)
    pdi = _make_series(N, [29.0, 27.0, 30.0])   # dip at idx1
    ndi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [22990.0] * N)
    rsi  = _make_series(N, [55.0, 58.0, 61.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c4"] is False


def test_c4_ce_fails_below_threshold_even_if_rising():
    """pdi rising but below 25 threshold → False."""
    df = _make_df(N)
    pdi = _make_series(N, [20.0, 22.0, 24.0])   # all < 25
    ndi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [22990.0] * N)
    rsi  = _make_series(N, [55.0, 58.0, 61.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c4"] is False


def test_c4_ce_fails_not_dominant_even_if_rising_and_above_threshold():
    """pdi > 25 and rising but ndi > pdi → dominance fails → False."""
    df = _make_df(N)
    pdi = _make_series(N, [26.0, 28.0, 30.0])
    ndi = _make_series(N, [32.0, 31.0, 31.0])   # ndi > pdi always
    vwap = _make_series(N, [22990.0] * N)
    rsi  = _make_series(N, [55.0, 58.0, 61.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c4"] is False


# ── C4 PE (ndi slope) ────────────────────────────────────────────────────────

def test_c4_pe_passes_strict_3candle_rise():
    """ndi strictly rising over 3 candles → pe_c4 True."""
    df = _make_df(N)
    ndi = _make_series(N, [26.0, 28.0, 30.0])
    pdi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [23010.0] * N)   # price below VWAP for PE
    rsi  = _make_series(N, [61.0, 58.0, 55.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["pe"]["c4"] is True


def test_c4_pe_fails_flat_middle():
    """ndi flat between idx2 and idx1 → pe_c4 False."""
    df = _make_df(N)
    ndi = _make_series(N, [26.0, 26.0, 30.0])
    pdi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [23010.0] * N)
    rsi  = _make_series(N, [61.0, 58.0, 55.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["pe"]["c4"] is False


# ── Guard: DI_TREND_CHECK=False bypasses slope ────────────────────────────────

def test_c4_di_trend_check_false_ignores_slope():
    """When DI_TREND_CHECK=False, a non-rising pdi still passes if threshold+dominance ok."""
    df = _make_df(N)
    pdi = _make_series(N, [29.0, 27.0, 30.0])   # dip — would fail slope check
    ndi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [22990.0] * N)
    rsi  = _make_series(N, [55.0, 58.0, 61.0])
    cfg = _cfg()
    cfg["DI_TREND_CHECK"] = False
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, cfg)
    assert result["ce"]["c4"] is True


# ── NaN guard ────────────────────────────────────────────────────────────────

def test_c4_nan_at_idx2_falls_to_false():
    """NaN at idx2 position → pdi_rising stays False → ce_c4 False."""
    df = _make_df(N)
    arr = np.full(N, np.nan)
    arr[N - 2] = 30.0   # idx0 only; idx1 and idx2 remain NaN
    arr[N - 3] = 28.0   # idx1 has value but idx2 still NaN
    pdi = pd.Series(arr)
    ndi = _make_series(N, [14.0, 13.0, 12.0])
    vwap = _make_series(N, [22990.0] * N)
    rsi  = _make_series(N, [55.0, 58.0, 61.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c4"] is False
