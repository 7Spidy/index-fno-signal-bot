"""Tests for C1: live price vs P0's close."""
import numpy as np
import pandas as pd

from src import signals

N = 30
CLOSE = 23000.0


def _make_df(n=N, close_val=CLOSE):
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":   np.full(n, close_val - 5),
        "high":   np.full(n, close_val + 10),
        "low":    np.full(n, close_val - 10),
        "close":  np.full(n, close_val),
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


def _eval(live_ltp):
    df   = _make_df(N, close_val=CLOSE)
    vwap = _warmup(N, CLOSE - 50, CLOSE - 20, CLOSE - 5)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    pdi  = _warmup(N, 20.0, 24.0, 26.0)
    ndi  = _warmup(N, 18.0, 16.0, 14.0)
    return signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(),
        live_ltp=live_ltp,
        live_vwap=CLOSE - 10,
        live_rsi=60.0,
        live_pdi=28.0,
        live_ndi=12.0,
    )


def test_c1_ce_true_when_live_above_p0_close():
    result = _eval(CLOSE + 10)
    assert result["ce"]["c1"] is True


def test_c1_pe_false_when_live_above_p0_close():
    result = _eval(CLOSE + 10)
    assert result["pe"]["c1"] is False


def test_c1_pe_true_when_live_below_p0_close():
    result = _eval(CLOSE - 10)
    assert result["pe"]["c1"] is True


def test_c1_ce_false_when_live_below_p0_close():
    result = _eval(CLOSE - 10)
    assert result["ce"]["c1"] is False


def test_c1_both_false_when_live_equals_p0_close():
    result = _eval(CLOSE)
    assert result["ce"]["c1"] is False
    assert result["pe"]["c1"] is False
