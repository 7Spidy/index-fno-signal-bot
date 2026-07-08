"""VWAP, RSI(14), DMI(14) — implemented from scratch per spec §7.
No pandas-ta or ta-lib. Wilder smoothing throughout.
"""
from datetime import datetime

import numpy as np
import pandas as pd


def vwap_session(df: pd.DataFrame, session_open: datetime) -> pd.Series:
    """Session-anchored VWAP using HLC3 typical price.

    Cumulates only from session_open onward. Prior candles get NaN.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    mask = df["timestamp"] >= session_open

    # Zero out pre-session so cumsum starts fresh from session_open
    tp_vol = (typical * df["volume"]).where(mask, 0.0)
    vol = df["volume"].where(mask, 0.0)

    cum_tp_vol = tp_vol.cumsum()
    cum_vol = vol.cumsum()

    vwap = cum_tp_vol / cum_vol
    vwap = vwap.where(mask)  # NaN for pre-session rows
    vwap.name = "vwap"
    return vwap


def rsi_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed RSI. Seed = simple average of first `period` changes."""
    close = df["close"].values.astype(float)
    n = len(close)
    rsi = np.full(n, np.nan)

    if n < period + 1:
        return pd.Series(rsi, index=df.index, name="rsi")

    deltas = np.diff(close)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)

    # Seed: simple average of first `period` gains/losses
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    alpha = 1.0 / period
    for i in range(period, n - 1):
        avg_gain = avg_gain * (1 - alpha) + gains[i] * alpha
        avg_loss = avg_loss * (1 - alpha) + losses[i] * alpha
        if avg_loss == 0.0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return pd.Series(rsi, index=df.index, name="rsi")


def dmi_wilder(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder-smoothed DMI: returns (+DI, -DI, ADX) as three Series."""
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    n = len(close)

    pdi_arr = np.full(n, np.nan)
    ndi_arr = np.full(n, np.nan)
    adx_arr = np.full(n, np.nan)

    if n < period + 1:
        return (
            pd.Series(pdi_arr, index=df.index, name="pdi"),
            pd.Series(ndi_arr, index=df.index, name="ndi"),
            pd.Series(adx_arr, index=df.index, name="adx"),
        )

    # Raw directional movement and true range per bar
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)

    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    # Wilder seed: sum of first `period` bars
    atr = tr[1:period + 1].sum()
    pdm = plus_dm[1:period + 1].sum()
    ndm = minus_dm[1:period + 1].sum()

    def _di(dm, atr_val):
        return 100.0 * dm / atr_val if atr_val != 0 else 0.0

    pdi_arr[period] = _di(pdm, atr)
    ndi_arr[period] = _di(ndm, atr)

    dx_vals = [_dx(pdi_arr[period], ndi_arr[period])]

    for i in range(period + 1, n):
        atr = atr - atr / period + tr[i]
        pdm = pdm - pdm / period + plus_dm[i]
        ndm = ndm - ndm / period + minus_dm[i]
        pdi_arr[i] = _di(pdm, atr)
        ndi_arr[i] = _di(ndm, atr)
        dx_vals.append(_dx(pdi_arr[i], ndi_arr[i]))

    # ADX = Wilder smoothing of DX over `period` bars
    # Seed at index 2*period - 1
    adx_start = 2 * period - 1
    if adx_start < n:
        adx_seed = np.mean(dx_vals[:period])
        adx_arr[adx_start] = adx_seed
        for j in range(period, len(dx_vals)):
            adx_arr[adx_start + (j - period) + 1] = (
                adx_arr[adx_start + (j - period)] * (period - 1) / period
                + dx_vals[j] / period
            )

    return (
        pd.Series(pdi_arr, index=df.index, name="pdi"),
        pd.Series(ndi_arr, index=df.index, name="ndi"),
        pd.Series(adx_arr, index=df.index, name="adx"),
    )


def _dx(pdi: float, ndi: float) -> float:
    denom = pdi + ndi
    if denom == 0:
        return 0.0
    return 100.0 * abs(pdi - ndi) / denom


def compute_supertrend(df: pd.DataFrame, period: int = 10,
                        multiplier: float = 3.0) -> tuple[pd.Series, pd.Series]:
    """Supertrend(period, multiplier) on a Wilder-smoothed ATR basis.

    Returns (value, direction):
      value     — the Supertrend line price level.
      direction — +1.0 while in an uptrend (line sits below price, acts as
                  support) or -1.0 while in a downtrend (line sits above
                  price, acts as resistance).
    Both are NaN before the ATR warm-up (first `period` bars) completes.
    """
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    n = len(close)

    st_val = np.full(n, np.nan)
    st_dir = np.full(n, np.nan)

    if n < period + 1:
        return (
            pd.Series(st_val, index=df.index, name="supertrend"),
            pd.Series(st_dir, index=df.index, name="supertrend_dir"),
        )

    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    # Wilder ATR: simple-average seed, then exponential smoothing (alpha=1/period) —
    # same seeding convention as rsi_wilder().
    atr = np.full(n, np.nan)
    atr[period] = tr[1:period + 1].mean()
    alpha = 1.0 / period
    for i in range(period + 1, n):
        atr[i] = atr[i - 1] * (1 - alpha) + tr[i] * alpha

    hl2 = (high + low) / 2.0
    final_ub = np.full(n, np.nan)
    final_lb = np.full(n, np.nan)

    start = period
    final_ub[start] = hl2[start] + multiplier * atr[start]
    final_lb[start] = hl2[start] - multiplier * atr[start]
    st_val[start] = final_ub[start]
    st_dir[start] = -1.0   # seed direction — no prior trend to compare against yet

    for i in range(start + 1, n):
        basic_ub = hl2[i] + multiplier * atr[i]
        basic_lb = hl2[i] - multiplier * atr[i]

        final_ub[i] = (basic_ub if (basic_ub < final_ub[i - 1] or close[i - 1] > final_ub[i - 1])
                       else final_ub[i - 1])
        final_lb[i] = (basic_lb if (basic_lb > final_lb[i - 1] or close[i - 1] < final_lb[i - 1])
                       else final_lb[i - 1])

        if st_dir[i - 1] == -1.0:
            if close[i] > final_ub[i]:
                st_dir[i] = 1.0
                st_val[i] = final_lb[i]
            else:
                st_dir[i] = -1.0
                st_val[i] = final_ub[i]
        else:
            if close[i] < final_lb[i]:
                st_dir[i] = -1.0
                st_val[i] = final_ub[i]
            else:
                st_dir[i] = 1.0
                st_val[i] = final_lb[i]

    return (
        pd.Series(st_val, index=df.index, name="supertrend"),
        pd.Series(st_dir, index=df.index, name="supertrend_dir"),
    )


def with_live_bar(df: pd.DataFrame, live_ltp: float) -> pd.DataFrame:
    """
    Returns a copy of df with its last row (the possibly-partial candle this
    codebase never trusts — see signals' P0 = iloc[-2] convention) dropped and
    replaced by one synthetic OHLC bar built from P0's close through the live
    price. Feed the result to rsi_wilder()/dmi_wilder() and read .iloc[-1] for
    a live-updated indicator value.

    Volume is irrelevant to RSI/DMI math (price-range only) and is set to 0.
    """
    p0_close = float(df.iloc[-2]["close"])
    live_bar = {
        "timestamp": df.iloc[-2]["timestamp"],
        "open":  p0_close,
        "high":  max(p0_close, live_ltp),
        "low":   min(p0_close, live_ltp),
        "close": live_ltp,
        "volume": 0,
    }
    base = df.iloc[:-1]   # drop the untrusted partial last row
    return pd.concat([base, pd.DataFrame([live_bar])], ignore_index=True)
