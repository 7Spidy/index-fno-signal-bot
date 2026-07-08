"""Tests for indicators.compute_supertrend() — Supertrend(period, multiplier)."""
import numpy as np
import pandas as pd

from src import indicators

PERIOD = 10
MULT = 3.0


def _trend_df(n=40, start=1000.0, step=2.0, spread=1.0):
    """Monotonic close series (rising if step>0, falling if step<0), with a
    tight high/low band around each close — a clean, unambiguous trend."""
    closes = start + np.arange(n) * step
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":      closes,
        "high":      closes + spread,
        "low":       closes - spread,
        "close":     closes,
        "volume":    np.ones(n) * 1000,
    })


def test_nan_before_warmup():
    df = _trend_df(n=40)
    st_val, st_dir = indicators.compute_supertrend(df, PERIOD, MULT)
    assert pd.isna(st_val.iloc[0])
    assert pd.isna(st_dir.iloc[0])
    assert pd.isna(st_val.iloc[PERIOD - 1])


def test_too_few_candles_returns_all_nan():
    df = _trend_df(n=5)
    st_val, st_dir = indicators.compute_supertrend(df, PERIOD, MULT)
    assert st_val.isna().all()
    assert st_dir.isna().all()


def test_uptrend_direction_is_positive():
    df = _trend_df(n=40, step=3.0)   # steadily rising close
    st_val, st_dir = indicators.compute_supertrend(df, PERIOD, MULT)
    assert st_dir.iloc[-1] == 1.0


def test_uptrend_value_sits_below_price():
    df = _trend_df(n=40, step=3.0)
    st_val, st_dir = indicators.compute_supertrend(df, PERIOD, MULT)
    assert float(st_val.iloc[-1]) < float(df["close"].iloc[-1])


def test_downtrend_direction_is_negative():
    df = _trend_df(n=40, step=-3.0)   # steadily falling close
    st_val, st_dir = indicators.compute_supertrend(df, PERIOD, MULT)
    assert st_dir.iloc[-1] == -1.0


def test_downtrend_value_sits_above_price():
    df = _trend_df(n=40, step=-3.0)
    st_val, st_dir = indicators.compute_supertrend(df, PERIOD, MULT)
    assert float(st_val.iloc[-1]) > float(df["close"].iloc[-1])


def test_return_lengths_match_input():
    df = _trend_df(n=40)
    st_val, st_dir = indicators.compute_supertrend(df, PERIOD, MULT)
    assert len(st_val) == len(df)
    assert len(st_dir) == len(df)
