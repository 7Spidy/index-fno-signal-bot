"""Tests for C3: live RSI direction (live > P0 > P1, or reverse)."""
import numpy as np
import pandas as pd

from src import signals

N = 30


def _make_df(n=N):
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  np.full(n, 23000.0),
        "high":  np.full(n, 23010.0),
        "low":   np.full(n, 22990.0),
        "close": np.full(n, 23000.0),
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


def _eval(rsi_series, live_rsi):
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    pdi  = _warmup(N, 23.0, 26.0, 28.0)
    ndi  = _warmup(N, 14.0, 13.0, 12.0)
    return signals.evaluate(
        df, vwap, rsi_series, pdi, ndi, _cfg(),
        live_ltp=23010.0,
        live_vwap=22990.0,
        live_rsi=live_rsi,
        live_pdi=28.0,
        live_ndi=12.0,
    )


# _warmup(N, r1_val, r0_val, last_row) → r1=arr[N-3], r0=arr[N-2]

# ── CE: live > P0 > P1 ───────────────────────────────────────────────────────

def test_c3_ce_passes_strictly_rising():
    rsi = _warmup(N, 50.0, 55.0, 58.0)   # r1=50, r0=55
    assert _eval(rsi, live_rsi=60.0)["ce"]["c3"] is True


def test_c3_ce_fails_flat_at_p0():
    # live=60 > r0=50 but r0=50 not > r1=50 (flat)
    rsi = _warmup(N, 50.0, 50.0, 52.0)   # r1=50, r0=50
    assert _eval(rsi, live_rsi=60.0)["ce"]["c3"] is False


def test_c3_ce_fails_dipping_p0_below_p1():
    # r0 < r1 → chain broken regardless of live
    rsi = _warmup(N, 55.0, 50.0, 52.0)   # r1=55, r0=50
    assert _eval(rsi, live_rsi=60.0)["ce"]["c3"] is False


def test_c3_ce_fails_live_not_above_p0():
    # live_rsi=54 < r0=55 → fails even though r0>r1
    rsi = _warmup(N, 50.0, 55.0, 58.0)   # r1=50, r0=55
    assert _eval(rsi, live_rsi=54.0)["ce"]["c3"] is False


# ── PE: live < P0 < P1 ───────────────────────────────────────────────────────

def test_c3_pe_passes_strictly_falling():
    rsi = _warmup(N, 60.0, 55.0, 52.0)   # r1=60, r0=55
    assert _eval(rsi, live_rsi=50.0)["pe"]["c3"] is True


def test_c3_pe_fails_flat_at_p0():
    # r0=60 not < r1=60 (flat) → fail
    rsi = _warmup(N, 60.0, 60.0, 58.0)   # r1=60, r0=60
    assert _eval(rsi, live_rsi=50.0)["pe"]["c3"] is False


def test_c3_pe_fails_live_not_below_p0():
    # live_rsi=56 > r0=55 → fails even though r0<r1
    rsi = _warmup(N, 60.0, 55.0, 52.0)   # r1=60, r0=55
    assert _eval(rsi, live_rsi=56.0)["pe"]["c3"] is False
