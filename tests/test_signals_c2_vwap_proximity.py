"""Tests for C2 VWAP proximity band sub-condition."""
import numpy as np
import pandas as pd
import pytest

from src import signals


def _make_df(n=30, close_val=23300.0):
    closes = np.full(n, close_val)
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":   closes - 5,
        "high":   closes + 10,
        "low":    closes - 10,
        "close":  closes,
        "volume": np.ones(n) * 1000,
    })


def _flat_series(n, val):
    return pd.Series(np.full(n, val))


def _warmup_series(n, final_vals):
    """Series with ≥15 non-null values; last len(final_vals) slots set to final_vals."""
    arr = np.full(n, 20.0)   # warm-up fill
    for i, v in enumerate(final_vals):
        arr[n - len(final_vals) + i] = v
    return pd.Series(arr)


def _cfg(instrument="NIFTY"):
    return {
        "MOMENTUM_RULE":             "close_gt_prev_close",
        "RSI_SLOPE_LOOKBACK":        3,
        "VWAP_CROSS_WINDOW_CANDLES": 6,
        "DI_THRESHOLD":              25,
        "REQUIRE_DI_DOMINANCE":      True,
        "DI_TREND_CHECK":            False,   # isolate C2
        "USE_ADX_FILTER":            False,
        "ADX_MIN":                   20,
        "COOLDOWN_CANDLES":          3,
        "SESSION_START_IST":         "09:15",
        "EVAL_WINDOW_IST":           ("09:40", "14:45"),
        "strike_step":               50,
        "instrument_name":           instrument,
        "VWAP_PROXIMITY_PTS":        {"NIFTY": 40, "BANKNIFTY": 200, "SENSEX": 160},
    }


N = 30
VWAP_LEVEL = 23300.0   # VWAP sits here


def _vwap_cross_up_series(n, vwap_level, current_close):
    """VWAP flat at vwap_level. Current close is above; one candle back was below."""
    arr = np.full(n, vwap_level)
    # All candles up to idx1 (n-3) have close below VWAP → cross detected
    return pd.Series(arr)


# ── CE: cross-up + within band ──────────────────────────────────────────────

def test_c2_ce_passes_within_band():
    """Price 20 pts above VWAP after cross-up — within 40 pt limit → pass."""
    close = VWAP_LEVEL + 20   # gap = 20, limit = 40
    df = _make_df(N, close_val=close)
    # Prior candle (idx1 = N-3) close was below VWAP to create a cross
    df.at[N - 3, "close"] = VWAP_LEVEL - 5
    vwap = _flat_series(N, VWAP_LEVEL)
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c2"] is True


def test_c2_ce_fails_outside_band():
    """Price 50 pts above VWAP after cross-up — exceeds 40 pt limit → fail."""
    close = VWAP_LEVEL + 50   # gap = 50, limit = 40
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = VWAP_LEVEL - 5
    vwap = _flat_series(N, VWAP_LEVEL)
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c2"] is False


def test_c2_ce_fails_at_exact_limit_plus_one():
    """Price exactly 41 pts above VWAP — one point over limit → fail."""
    close = VWAP_LEVEL + 41
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = VWAP_LEVEL - 5
    vwap = _flat_series(N, VWAP_LEVEL)
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c2"] is False


def test_c2_ce_passes_at_exact_limit():
    """Price exactly 40 pts above VWAP — at the limit → pass."""
    close = VWAP_LEVEL + 40
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = VWAP_LEVEL - 5
    vwap = _flat_series(N, VWAP_LEVEL)
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c2"] is True


# ── CE: within band but NO cross ────────────────────────────────────────────

def test_c2_ce_fails_within_band_no_cross():
    """Price within band but never crossed — has been above VWAP all window → fail."""
    close = VWAP_LEVEL + 20
    df = _make_df(N, close_val=close)
    # All historical candles also above VWAP — no cross
    vwap = _flat_series(N, VWAP_LEVEL)   # close always > vwap, no prior candle below
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["ce"]["c2"] is False


# ── PE: cross-down + within band ────────────────────────────────────────────

def test_c2_pe_passes_within_band():
    """Price 20 pts below VWAP after cross-down — within 40 pt limit → pass."""
    close = VWAP_LEVEL - 20
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = VWAP_LEVEL + 5   # prior candle above VWAP
    vwap = _flat_series(N, VWAP_LEVEL)
    rsi  = _warmup_series(N, [61.0, 58.0, 55.0])
    pdi  = _warmup_series(N, [14.0, 13.0, 12.0])
    ndi  = _warmup_series(N, [26.0, 28.0, 30.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["pe"]["c2"] is True


def test_c2_pe_fails_outside_band():
    """Price 60 pts below VWAP — exceeds 40 pt limit → fail."""
    close = VWAP_LEVEL - 60
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = VWAP_LEVEL + 5
    vwap = _flat_series(N, VWAP_LEVEL)
    rsi  = _warmup_series(N, [61.0, 58.0, 55.0])
    pdi  = _warmup_series(N, [14.0, 13.0, 12.0])
    ndi  = _warmup_series(N, [26.0, 28.0, 30.0])
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, _cfg())
    assert result["pe"]["c2"] is False


# ── BANKNIFTY: wider band ────────────────────────────────────────────────────

def test_c2_banknifty_passes_within_200():
    """BANKNIFTY price 180 pts above VWAP — within 200 pt limit → pass."""
    vwap_bnk = 55000.0
    close = vwap_bnk + 180
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = vwap_bnk - 10
    vwap = _flat_series(N, vwap_bnk)
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    cfg  = _cfg("BANKNIFTY")
    cfg["strike_step"] = 100
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, cfg)
    assert result["ce"]["c2"] is True


def test_c2_banknifty_fails_outside_200():
    """BANKNIFTY price 210 pts above VWAP — exceeds 200 pt limit → fail."""
    vwap_bnk = 55000.0
    close = vwap_bnk + 210
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = vwap_bnk - 10
    vwap = _flat_series(N, vwap_bnk)
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    cfg  = _cfg("BANKNIFTY")
    cfg["strike_step"] = 100
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, cfg)
    assert result["ce"]["c2"] is False


# ── Unknown instrument falls back to inf (no proximity filter) ───────────────

def test_c2_unknown_instrument_no_proximity_filter():
    """Unknown instrument name → proximity_limit = inf → only cross matters."""
    close = VWAP_LEVEL + 999   # very far, but no limit configured
    df = _make_df(N, close_val=close)
    df.at[N - 3, "close"] = VWAP_LEVEL - 5
    vwap = _flat_series(N, VWAP_LEVEL)
    rsi  = _warmup_series(N, [55.0, 58.0, 61.0])
    pdi  = _warmup_series(N, [26.0, 28.0, 30.0])
    ndi  = _warmup_series(N, [14.0, 13.0, 12.0])
    cfg  = _cfg("UNKNOWN_INDEX")
    result = signals.evaluate(df, vwap, rsi, pdi, ndi, cfg)
    assert result["ce"]["c2"] is True
