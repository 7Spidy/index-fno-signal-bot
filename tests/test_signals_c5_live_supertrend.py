"""Tests for C5: Supertrend(10,5) live direction — soft/informational only."""
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


def _base_kwargs():
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 50.0, 50.0)
    pdi  = _warmup(N, 10.0, 10.0, 10.0)
    ndi  = _warmup(N, 10.0, 10.0, 10.0)
    return dict(
        df=df, vwap=vwap, rsi=rsi, pdi=pdi, ndi=ndi, cfg=_cfg(),
        live_ltp=23000.0, live_vwap=22990.0,
        live_rsi=50.0, live_pdi=10.0, live_ndi=10.0,
    )


# ── Direction mapping ────────────────────────────────────────────────────────

def test_c5_uptrend_sets_ce_true_pe_false():
    result = signals.evaluate(**_base_kwargs(), live_supertrend_dir=True)
    assert result["ce"]["c5"] is True
    assert result["pe"]["c5"] is False


def test_c5_downtrend_sets_ce_false_pe_true():
    result = signals.evaluate(**_base_kwargs(), live_supertrend_dir=False)
    assert result["ce"]["c5"] is False
    assert result["pe"]["c5"] is True


def test_c5_none_sets_both_false():
    result = signals.evaluate(**_base_kwargs(), live_supertrend_dir=None)
    assert result["ce"]["c5"] is False
    assert result["pe"]["c5"] is False


def test_c5_defaults_to_none_when_not_passed():
    result = signals.evaluate(**_base_kwargs())
    assert result["ce"]["c5"] is False
    assert result["pe"]["c5"] is False


# ── Critical regression guard ────────────────────────────────────────────────

def test_c5_false_never_suppresses_a_real_ce_signal():
    # Same fixture as test_c4_ce_passes_index_threshold_25 (all of C1-C4 pass),
    # but with a downtrend Supertrend (C5 == False for CE). ce_signal must
    # still fire — C5 is soft and must never enter the gating AND-chain.
    df   = _make_df()
    vwap = _warmup(N, 22990.0, 22990.0, 22990.0)
    rsi  = _warmup(N, 50.0, 55.0, 58.0)
    pdi  = _warmup(N, 23.0, 26.0, 28.0)
    ndi  = _warmup(N, 14.0, 13.0, 12.0)
    result = signals.evaluate(
        df, vwap, rsi, pdi, ndi, _cfg(25),
        live_ltp=23010.0, live_vwap=22990.0,
        live_rsi=60.0, live_pdi=28.0, live_ndi=12.0,
        live_supertrend_dir=False,
    )
    assert result["ce"]["c1"] is True
    assert result["ce"]["c2"] is True
    assert result["ce"]["c3"] is True
    assert result["ce"]["c4"] is True
    assert result["ce"]["c5"] is False
    assert result["ce"]["signal"] is True
