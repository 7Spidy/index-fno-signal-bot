"""Tests for signals.evaluate_stock_supertrend_vwap() — the 14-stock
Supertrend(10,3) + VWAP pullback entry, independent of the frozen index
C1-C4 evaluate() path."""
import numpy as np
import pandas as pd

from src import signals

N = 15
CLOSE = 1000.0
TOUCH_PCT = 0.0015


def _make_df(n=N, close_val=CLOSE):
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":   np.full(n, close_val - 1),
        "high":   np.full(n, close_val + 2),
        "low":    np.full(n, close_val - 2),
        "close":  np.full(n, close_val),
        "volume": np.ones(n) * 1000,
    })


def _const_series(n, value):
    return pd.Series(np.full(n, value))


def _cfg():
    return {"strike_step": 50, "STOCK_VWAP_TOUCH_PCT": TOUCH_PCT}


def _eval(vwap0, st_dir0, live_ltp, live_vwap, live_st_dir, live_st_value=990.0):
    df = _make_df()
    vwap = _const_series(N, vwap0)
    st_dir = _const_series(N, st_dir0)
    return signals.evaluate_stock_supertrend_vwap(
        df, vwap, st_dir,
        live_ltp=live_ltp, live_vwap=live_vwap,
        live_st_dir=live_st_dir, live_st_value=live_st_value,
        cfg=_cfg(),
    )


# ── CE ────────────────────────────────────────────────────────────────────

def test_ce_signal_fires_when_all_conditions_met():
    # P0 close (CLOSE) touches vwap0 (within 0.15%), ST up at P0, live
    # confirms above live_vwap with ST still up.
    result = _eval(vwap0=CLOSE * 1.0005, st_dir0=1.0,
                    live_ltp=CLOSE + 5, live_vwap=CLOSE, live_st_dir=1.0)
    assert result["ce"]["c1"] is True
    assert result["ce"]["c2"] is True
    assert result["ce"]["c3"] is True
    assert result["ce"]["c4"] is True
    assert result["ce"]["signal"] is True
    assert result["pe"]["signal"] is False


def test_ce_blocked_when_supertrend_down_at_p0():
    result = _eval(vwap0=CLOSE, st_dir0=-1.0,
                    live_ltp=CLOSE + 5, live_vwap=CLOSE, live_st_dir=1.0)
    assert result["ce"]["c1"] is False
    assert result["ce"]["signal"] is False


def test_ce_blocked_when_p0_close_far_from_vwap():
    # 5% away — well outside the 0.15% touch band.
    result = _eval(vwap0=CLOSE * 1.05, st_dir0=1.0,
                    live_ltp=CLOSE + 5, live_vwap=CLOSE, live_st_dir=1.0)
    assert result["ce"]["c2"] is False
    assert result["ce"]["signal"] is False


def test_ce_blocked_when_live_price_below_live_vwap():
    result = _eval(vwap0=CLOSE, st_dir0=1.0,
                    live_ltp=CLOSE - 5, live_vwap=CLOSE, live_st_dir=1.0)
    assert result["ce"]["c3"] is False
    assert result["ce"]["signal"] is False


def test_ce_blocked_when_supertrend_flips_down_on_confirmation():
    result = _eval(vwap0=CLOSE, st_dir0=1.0,
                    live_ltp=CLOSE + 5, live_vwap=CLOSE, live_st_dir=-1.0)
    assert result["ce"]["c4"] is False
    assert result["ce"]["signal"] is False


# ── PE (mirror) ──────────────────────────────────────────────────────────

def test_pe_signal_fires_when_all_conditions_met():
    result = _eval(vwap0=CLOSE * 0.9995, st_dir0=-1.0,
                    live_ltp=CLOSE - 5, live_vwap=CLOSE, live_st_dir=-1.0)
    assert result["pe"]["c1"] is True
    assert result["pe"]["c2"] is True
    assert result["pe"]["c3"] is True
    assert result["pe"]["c4"] is True
    assert result["pe"]["signal"] is True
    assert result["ce"]["signal"] is False


def test_pe_blocked_when_supertrend_up_at_p0():
    result = _eval(vwap0=CLOSE, st_dir0=1.0,
                    live_ltp=CLOSE - 5, live_vwap=CLOSE, live_st_dir=-1.0)
    assert result["pe"]["c1"] is False
    assert result["pe"]["signal"] is False


# ── initial_sl / warm-up ─────────────────────────────────────────────────

def test_initial_sl_is_live_supertrend_value():
    result = _eval(vwap0=CLOSE * 1.0005, st_dir0=1.0,
                    live_ltp=CLOSE + 5, live_vwap=CLOSE, live_st_dir=1.0,
                    live_st_value=985.5)
    assert result["initial_sl"] == 985.5


def test_insufficient_warmup_returns_empty_result():
    df = _make_df()
    vwap = pd.Series(np.full(N, np.nan))     # never warmed up
    st_dir = pd.Series(np.full(N, np.nan))
    result = signals.evaluate_stock_supertrend_vwap(
        df, vwap, st_dir,
        live_ltp=CLOSE + 5, live_vwap=CLOSE, live_st_dir=1.0, live_st_value=990.0,
        cfg=_cfg(),
    )
    assert result["ce"]["signal"] is False
    assert result["pe"]["signal"] is False
    assert result["initial_sl"] is None


def test_missing_live_quote_returns_empty_result():
    df = _make_df()
    vwap = _const_series(N, CLOSE)
    st_dir = _const_series(N, 1.0)
    result = signals.evaluate_stock_supertrend_vwap(
        df, vwap, st_dir,
        live_ltp=None, live_vwap=None, live_st_dir=1.0, live_st_value=990.0,
        cfg=_cfg(),
    )
    assert result["ce"]["signal"] is False
    assert result["pe"]["signal"] is False


def test_returns_same_shape_as_index_evaluate():
    """Blocking requirement: the ce/pe sub-dict shape must match the
    index C1-C4 evaluate()'s shape field-for-field (c1..c4 + signal)."""
    result = _eval(vwap0=CLOSE, st_dir0=1.0,
                    live_ltp=CLOSE + 5, live_vwap=CLOSE, live_st_dir=1.0)
    for side in ("ce", "pe"):
        assert set(result[side].keys()) == {"c1", "c2", "c3", "c4", "signal"}
    for key in ("futures_price", "candle_high", "candle_low", "prev_candle_high",
                "prev_candle_low", "candle_time", "vwap", "live_price",
                "live_vwap", "atm_strike", "initial_sl"):
        assert key in result
