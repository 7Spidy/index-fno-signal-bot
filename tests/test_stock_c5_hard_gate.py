"""
Unit tests: Supertrend (C5) is now a HARD GATE on ce_signal/pe_signal for
stocks (src/stock_main.py._evaluate), as of the 2026-07-18 change spec.

The index path (src/signals.py) is untouched and keeps C5 informational —
covered separately by tests/test_signals_c5_live_supertrend.py.

All indicator computation inside _evaluate is mocked so this test controls
C1-C5 directly rather than reverse-engineering real indicator math.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

# ── Stub kiteconnect/dotenv so importing src.stock_main has no external deps ──
if "kiteconnect" not in sys.modules:
    _kc_stub = types.ModuleType("kiteconnect")
    _kc_stub.KiteConnect = MagicMock
    sys.modules["kiteconnect"] = _kc_stub

if "dotenv" not in sys.modules:
    _dotenv_stub = types.ModuleType("dotenv")
    _dotenv_stub.load_dotenv = lambda: None
    sys.modules["dotenv"] = _dotenv_stub

from src import indicators, stock_main  # noqa: E402
from src import stock_config as cfg  # noqa: E402

_STOCK = cfg.STOCK_BY_NAME["RELIANCE"]


def _make_df(n=5):
    ts = pd.date_range("2026-07-18 09:15", periods=n, freq="5min")
    return pd.DataFrame({
        "timestamp": ts,
        "open":  np.full(n, 100.0),
        "high":  np.full(n, 102.0),
        "low":   np.full(n, 95.0),
        "close": np.full(n, 100.0),
        "volume": np.full(n, 1000.0),
    })


def _run_evaluate(live_supertrend_dir):
    """Runs _evaluate with all indicator math mocked. C1-C4 are all forced
    True for both CE and PE setup values below; only C5 varies."""
    df = _make_df()
    n = len(df)

    # C1: live_ltp > p0.close (CE) — set live_ltp high, p0.close low.
    live_ltp = 110.0

    # C2: live_ltp > live_vwap and p0.low <= v0
    live_vwap = 105.0
    vwap_s = pd.Series(np.full(n, 100.0))  # v0 = 100 >= p0.low(95)

    # C3: live_rsi > r0 > r1
    rsi_s = pd.Series([40, 45, 50, 55, 50])  # r0=iloc[-2]=55, r1=iloc[-3]=50
    live_rsi = 60.0

    # C4: live_pdi > di_threshold(24), > live_ndi, > pdi0 > pdi1
    pdi_s = pd.Series([10, 15, 20, 28, 20])  # pdi0=28, pdi1=20
    ndi_s = pd.Series([10, 10, 10, 10, 10])
    live_pdi = 30.0
    live_ndi = 5.0

    live_quotes = {
        f"{_STOCK['spot_exchange']}:{_STOCK['equity_symbol']}": {
            "ltp": live_ltp, "vwap": live_vwap,
        }
    }

    live_st_dir_s = pd.Series([live_supertrend_dir])

    with (
        patch.object(indicators, "dmi_wilder", side_effect=[
            (pdi_s, ndi_s, None), (pd.Series([live_pdi]), pd.Series([live_ndi]), None),
        ]),
        patch.object(indicators, "rsi_wilder", side_effect=[
            rsi_s, pd.Series([live_rsi]),
        ]),
        patch.object(indicators, "vwap_session", return_value=vwap_s),
        patch.object(indicators, "with_live_bar", return_value=df),
        patch.object(indicators, "supertrend_wilder", return_value=(None, live_st_dir_s)),
    ):
        return stock_main._evaluate(_STOCK, df, live_quotes)


def test_ce_signal_false_when_c5_false_even_if_c1_to_c4_all_true():
    result = _run_evaluate(live_supertrend_dir=False)
    assert result["ce"]["c1"] is True
    assert result["ce"]["c2"] is True
    assert result["ce"]["c3"] is True
    assert result["ce"]["c4"] is True
    assert result["ce"]["c5"] is False
    assert result["ce"]["signal"] is False, (
        "ce_signal must be False when C5 (Supertrend) fails, even with C1-C4 all True"
    )


def test_ce_signal_true_when_c5_true_and_c1_to_c4_all_true():
    result = _run_evaluate(live_supertrend_dir=True)
    assert result["ce"]["c5"] is True
    assert result["ce"]["signal"] is True


def test_pe_signal_false_when_c5_false_even_if_c1_to_c4_all_true():
    """Mirror setup, but flip C1-C4 to bearish while forcing pe_c5 False by
    passing live_supertrend_dir=True (pe_c5 = live_supertrend_dir is False)."""
    result = _run_evaluate(live_supertrend_dir=True)
    assert result["pe"]["c5"] is False
    assert result["pe"]["signal"] is False


def test_pe_signal_true_when_c5_true_and_c1_to_c4_all_true():
    result = _run_evaluate(live_supertrend_dir=False)
    assert result["pe"]["c5"] is True
    # pe c1-c4 are all False in this fixture (bullish setup), so pe_signal
    # stays False regardless of c5 — this asserts c5=True doesn't force a
    # signal on its own, the AND-chain still requires c1-c4.
    assert result["pe"]["signal"] is False
