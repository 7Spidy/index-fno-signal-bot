"""Tests for indicators.supertrend_wilder() — Wilder-smoothed Supertrend(10,5)."""
import numpy as np
import pandas as pd

from src import indicators

PERIOD = 10
MULTIPLIER = 5.0


def _flat_df(n, close_val=100.0, hl_range=1.0):
    """Flat OHLC series: high = close+range, low = close-range."""
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  np.full(n, close_val),
        "high":  np.full(n, close_val + hl_range),
        "low":   np.full(n, close_val - hl_range),
        "close": np.full(n, close_val),
        "volume": np.ones(n) * 1000,
    })


# ── Warmup ────────────────────────────────────────────────────────────────────

def test_warmup_first_period_rows_are_nan():
    df = _flat_df(25)
    st_line, in_uptrend = indicators.supertrend_wilder(df, PERIOD, MULTIPLIER)
    assert st_line.iloc[:PERIOD].isna().all()
    assert all(v is None for v in in_uptrend.iloc[:PERIOD])
    assert pd.notna(st_line.iloc[PERIOD])
    assert in_uptrend.iloc[PERIOD] is not None


def test_short_df_returns_all_nan():
    df = _flat_df(PERIOD)  # exactly `period` rows — insufficient warmup
    st_line, in_uptrend = indicators.supertrend_wilder(df, PERIOD, MULTIPLIER)
    assert st_line.isna().all()
    assert all(v is None for v in in_uptrend)


# ── Known-trend: steadily rising closes ─────────────────────────────────────

def test_steadily_rising_closes_settle_uptrend():
    n = 25
    closes = np.linspace(100, 130, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  closes,
        "high":  closes + 1,
        "low":   closes - 1,
        "close": closes,
        "volume": np.ones(n) * 1000,
    })
    st_line, in_uptrend = indicators.supertrend_wilder(df, PERIOD, MULTIPLIER)
    assert all(v is True for v in in_uptrend.iloc[PERIOD:])


# ── Flip test: flat uptrend, then a sharp drop forces exactly one flip ──────

def test_flip_occurs_exactly_once_at_expected_index():
    n = 30
    drop_idx = 25
    close = np.full(n, 100.0)
    high  = np.full(n, 101.0)
    low   = np.full(n, 99.0)

    # Sharp drop at drop_idx and a few flat candles at the new lower level.
    close[drop_idx:] = 50.0
    high[drop_idx:]  = 51.0
    low[drop_idx:]   = 49.0

    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  close,
        "high":  high,
        "low":   low,
        "close": close,
        "volume": np.ones(n) * 1000,
    })

    st_line, in_uptrend = indicators.supertrend_wilder(df, PERIOD, MULTIPLIER)

    dirs = list(in_uptrend.iloc[PERIOD:])
    # Before the drop: uptrend. At/after the drop: downtrend.
    pre_drop  = list(in_uptrend.iloc[PERIOD:drop_idx])
    post_drop = list(in_uptrend.iloc[drop_idx:])
    assert all(v is True for v in pre_drop)
    assert all(v is False for v in post_drop)

    # Exactly one flip across the whole series.
    flips = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
    assert flips == 1


def test_bands_ratchet_up_never_down_during_uptrend():
    n = 25
    closes = np.linspace(100, 130, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  closes,
        "high":  closes + 1,
        "low":   closes - 1,
        "close": closes,
        "volume": np.ones(n) * 1000,
    })
    st_line, _ = indicators.supertrend_wilder(df, PERIOD, MULTIPLIER)
    line_vals = st_line.iloc[PERIOD:].values
    # In an uptrend, the plotted line is the (ratcheting) lower band — it must
    # never decrease.
    assert all(line_vals[i + 1] >= line_vals[i] - 1e-9 for i in range(len(line_vals) - 1))
