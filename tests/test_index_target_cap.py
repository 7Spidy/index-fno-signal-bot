"""
Unit tests: ATR-anchored clamp for index SL/target (2026-07-29 change spec).

Covers:
- indicators.atr_wilder() — standalone Wilder ATR, mirrors supertrend_wilder's
  internal calc without touching that function's signature/behavior.
- config.ATR_TARGET_K_INDEX placeholder constant.
- The clamp formula main.py now uses in place of the raw structural-candle-gap
  sizing (prev_candle_high/low), reproduced here since the logic lives inline
  in main()'s per-signal block rather than a standalone function — same
  approach as tests/test_stock_target.py takes for stock_main.py.
"""
import numpy as np
import pandas as pd
import pytest

from src import config
from src import indicators

PERIOD = 10


def _flat_df(n, close_val=100.0, hl_range=1.0):
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  np.full(n, close_val),
        "high":  np.full(n, close_val + hl_range),
        "low":   np.full(n, close_val - hl_range),
        "close": np.full(n, close_val),
        "volume": np.ones(n) * 1000,
    })


# ── atr_wilder() ─────────────────────────────────────────────────────────────

def test_atr_wilder_warmup_is_nan_until_period():
    df = _flat_df(25)
    atr = indicators.atr_wilder(df, PERIOD)
    assert atr.iloc[:PERIOD].isna().all()
    assert pd.notna(atr.iloc[PERIOD])


def test_atr_wilder_short_df_returns_all_nan():
    df = _flat_df(PERIOD)  # exactly `period` rows — insufficient warmup
    atr = indicators.atr_wilder(df, PERIOD)
    assert atr.isna().all()


def test_atr_wilder_constant_true_range_settles_at_that_value():
    # Flat hl_range of 2.0 => true range is constant at 4.0 (high - low) for
    # every bar after the first, so Wilder-smoothed ATR should converge to 4.0.
    df = _flat_df(30, hl_range=2.0)
    atr = indicators.atr_wilder(df, PERIOD)
    assert atr.iloc[PERIOD] == pytest.approx(4.0)
    assert atr.iloc[-1] == pytest.approx(4.0)


def test_atr_wilder_does_not_mutate_supertrend_wilder_signature():
    import inspect
    sig = inspect.signature(indicators.supertrend_wilder)
    assert list(sig.parameters) == ["df", "period", "multiplier"]
    df = _flat_df(25)
    result = indicators.supertrend_wilder(df, PERIOD, 5.0)
    assert len(result) == 2  # still a 2-tuple


# ── config constants ─────────────────────────────────────────────────────────

def test_atr_target_k_index_defined():
    assert config.ATR_TARGET_K_INDEX == 3.0


def test_no_new_ceiling_constant_reuses_option_cache_range():
    assert not hasattr(config, "TARGET_CEILING_PCT_INDEX")
    assert set(config.OPTION_CACHE_RANGE) >= {"NIFTY", "BANKNIFTY", "SENSEX"}


# ── Clamp formula (mirrors src/main.py's per-signal SL/target block) ────────

def _compute_target(name, live_atr, min_risk, rr):
    floor_pts = min_risk
    ceiling_pts = 0.8 * config.OPTION_CACHE_RANGE[name]
    if live_atr:
        raw_target_pts = config.ATR_TARGET_K_INDEX * live_atr
        target_pts = max(floor_pts, min(raw_target_pts, ceiling_pts))
        target_source = "atr"
    else:
        target_pts = floor_pts * rr
        target_source = "fallback_floor_rr"
    spot_risk_pts = round(target_pts / rr, 2) if rr > 0 else round(target_pts, 2)
    return target_pts, spot_risk_pts, target_source


def test_wide_range_candle_no_longer_balloons_target():
    """Regression for the 2026-07-29 13:45 BANKNIFTY PE case: a ~213pt
    structural candle gap must NOT translate into an uncapped target anymore —
    it should be clamped to ceiling_pts regardless of how wide the candle is."""
    name, min_risk, rr = "BANKNIFTY", 30, config.TARGET_RR
    ceiling_pts = 0.8 * config.OPTION_CACHE_RANGE[name]

    # A huge ATR (proxy for the wide-range candle) would blow way past ceiling
    # under the raw ATR_TARGET_K_INDEX multiplier if unclamped: 3.0 * 500 = 1500pts,
    # vs. a ceiling of 0.8 * 1500 = 1200pts.
    huge_atr = 500.0
    target_pts, spot_risk_pts, source = _compute_target(name, huge_atr, min_risk, rr)

    assert source == "atr"
    assert target_pts == ceiling_pts
    assert target_pts <= ceiling_pts


def test_small_atr_respects_floor_not_raw_atr():
    name, min_risk, rr = "NIFTY", 10, config.TARGET_RR
    tiny_atr = 1.0  # ATR_TARGET_K_INDEX * 1.0 = 3.0, below floor of 10
    target_pts, spot_risk_pts, source = _compute_target(name, tiny_atr, min_risk, rr)

    assert source == "atr"
    assert target_pts == min_risk


def test_missing_atr_falls_back_to_floor_times_rr():
    name, min_risk, rr = "SENSEX", 30, config.TARGET_RR
    target_pts, spot_risk_pts, source = _compute_target(name, None, min_risk, rr)

    assert source == "fallback_floor_rr"
    assert target_pts == min_risk * rr


def test_spot_sl_and_target_land_within_ceiling_of_reference():
    name, min_risk, rr = "BANKNIFTY", 30, config.TARGET_RR
    reference = 51000.0
    ceiling_pts = 0.8 * config.OPTION_CACHE_RANGE[name]

    target_pts, spot_risk_pts, _ = _compute_target(name, 500.0, min_risk, rr)
    spot_sl_ce = reference - spot_risk_pts
    spot_tgt_ce = reference + target_pts

    assert abs(spot_tgt_ce - reference) <= ceiling_pts
    assert abs(spot_sl_ce - reference) <= ceiling_pts
