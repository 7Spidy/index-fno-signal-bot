"""Tests for C4: DI threshold, dominance, and live > P0 > P1 rising check."""
import numpy as np
import pandas as pd

from src import signals

N = 30


def _make_df(n=N, close_val=23000.0):
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  np.full(n, close_val - 5),
        "high":  np.full(n, close_val + 10),
        "low":   np.full(n, close_val - 10),
        "close": np.full(n, close_val),
        "volume": np.ones(n) * 1000,
    })


def _warmup(n, *tail):
    # Fills with 5.0 warm-up so ≥15 non-null values satisfy the guard.
    arr = np.full(n, 5.0)
    for i, v in enumerate(tail):
        arr[n - len(tail) + i] = v
    return pd.Series(arr)


def _cfg(di_threshold=25, di_trend_check=True):
    return {
        "DI_THRESHOLD": di_threshold,
        "REQUIRE_DI_DOMINANCE": True,
        "DI_TREND_CHECK": di_trend_check,
        "strike_step": 50,
    }


# _warmup(N, pdi1, pdi0, last_row) → pdi1=arr[N-3]=P1, pdi0=arr[N-2]=P0


# ── CE: index threshold = 25 ─────────────────────────────────────────────────

def test_c4_ce_passes_index_threshold_25():
    # live_pdi=28 > 25, 28 > live_ndi=12, 28 > pdi0=26 > pdi1=23 ✓
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    pdi  = _warmup(N, 23.0, 26.0, 28.0)
    ndi  = _warmup(N, 14.0, 13.0, 12.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23010.0, live_vwap=22990.0,
        live_rsi=60.0, live_pdi=28.0, live_ndi=12.0,
    )
    assert result["ce"]["c4"] is True


def test_c4_ce_fails_below_index_threshold_25():
    # live_pdi=24.9 < 25 → threshold fails
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    pdi  = _warmup(N, 20.0, 22.0, 24.0)
    ndi  = _warmup(N, 14.0, 13.0, 12.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23010.0, live_vwap=22990.0,
        live_rsi=60.0, live_pdi=24.9, live_ndi=12.0,
    )
    assert result["ce"]["c4"] is False


# ── CE: stock threshold = 24 ─────────────────────────────────────────────────

def test_c4_ce_passes_stock_threshold_24():
    # Same live_pdi=24.9, now threshold=24 → passes
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    pdi  = _warmup(N, 20.0, 22.0, 24.0)
    ndi  = _warmup(N, 14.0, 13.0, 12.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(24),
        live_ltp=23010.0, live_vwap=22990.0,
        live_rsi=60.0, live_pdi=24.9, live_ndi=12.0,
    )
    assert result["ce"]["c4"] is True


# ── CE: dominance check ───────────────────────────────────────────────────────

def test_c4_ce_fails_dominance_when_live_ndi_larger():
    # live_pdi=28 < live_ndi=35 → dominance fails
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    pdi  = _warmup(N, 23.0, 26.0, 28.0)
    ndi  = _warmup(N, 30.0, 29.0, 28.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23010.0, live_vwap=22990.0,
        live_rsi=60.0, live_pdi=28.0, live_ndi=35.0,
    )
    assert result["ce"]["c4"] is False


# ── CE: live > P0 > P1 rising check ─────────────────────────────────────────

def test_c4_ce_fails_dip_at_p1_breaks_chain():
    # pdi1=29, pdi0=26 → 28 > 26 but 26 > 29 is False → pdi_rising=False
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    pdi  = _warmup(N, 29.0, 26.0, 27.0)
    ndi  = _warmup(N, 14.0, 13.0, 12.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23010.0, live_vwap=22990.0,
        live_rsi=60.0, live_pdi=28.0, live_ndi=12.0,
    )
    assert result["ce"]["c4"] is False


def test_c4_ce_nan_at_p1_keeps_pdi_rising_false():
    # pdi1=NaN → if block skipped → pdi_rising stays False → ce_c4=False
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    arr  = np.full(N, 20.0)   # 15+ non-null to pass guard
    arr[N - 3] = np.nan        # P1 is NaN
    arr[N - 2] = 26.0          # P0 has value
    pdi  = pd.Series(arr)
    ndi  = _warmup(N, 14.0, 13.0, 12.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23010.0, live_vwap=22990.0,
        live_rsi=60.0, live_pdi=28.0, live_ndi=12.0,
    )
    assert result["ce"]["c4"] is False


# ── PE: live > P0 > P1 rising check (ndi) ───────────────────────────────────

def test_c4_pe_passes_with_rising_ndi():
    # live_ndi=30 > ndi0=28 > ndi1=26, all > 25, ndi > pdi ✓
    df   = _make_df(close_val=23050.0)
    vwap = _warmup(N, 23060.0, 23060.0, 23060.0)
    rsi  = _warmup(N, 60.0, 55.0, 52.0)
    pdi  = _warmup(N, 14.0, 13.0, 12.0)
    ndi  = _warmup(N, 26.0, 28.0, 30.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23040.0, live_vwap=23060.0,
        live_rsi=50.0, live_pdi=12.0, live_ndi=30.0,
    )
    assert result["pe"]["c4"] is True


def test_c4_pe_fails_dip_at_p1_breaks_chain():
    # ndi1=30, ndi0=27 → 30>27 but 27>30 is False → ndi_rising=False
    df   = _make_df(close_val=23050.0)
    vwap = _warmup(N, 23060.0, 23060.0, 23060.0)
    rsi  = _warmup(N, 60.0, 55.0, 52.0)
    pdi  = _warmup(N, 14.0, 13.0, 12.0)
    ndi  = _warmup(N, 30.0, 27.0, 28.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23040.0, live_vwap=23060.0,
        live_rsi=50.0, live_pdi=12.0, live_ndi=30.0,
    )
    assert result["pe"]["c4"] is False
