"""
Unit test: stock_main.main() triggers position_tracker.run_heartbeat()
immediately after writing a tracker intent for each fired signal, instead of
waiting for the externally-scheduled trade-tracker.yml run (2026-07-18 change
spec, step 4). No real Redis/Kite/git calls are made — everything main()
touches is mocked.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

if "kiteconnect" not in sys.modules:
    _kc_stub = types.ModuleType("kiteconnect")
    _kc_stub.KiteConnect = MagicMock
    sys.modules["kiteconnect"] = _kc_stub

if "dotenv" not in sys.modules:
    _dotenv_stub = types.ModuleType("dotenv")
    _dotenv_stub.load_dotenv = lambda: None
    sys.modules["dotenv"] = _dotenv_stub

from src import stock_main  # noqa: E402
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


def _fired_ce_result():
    return {
        "name": "RELIANCE", "sector": "Energy/Conglomerate", "lot_size": 250,
        "ce": {"c1": True, "c2": True, "c3": True, "c4": True, "c5": True, "signal": True},
        "pe": {"c1": False, "c2": False, "c3": False, "c4": False, "c5": False, "signal": False},
        "futures_price": 1400.0, "candle_high": 1410.0, "candle_low": 1390.0,
        "prev_candle_high": 1405.0, "prev_candle_low": 1385.0,
        "candle_time": "10:10 IST", "vwap": 1395.0, "rsi": 60.0,
        "pdi": 30.0, "ndi": 5.0,
        "live_price": 1402.0, "live_vwap": 1396.0, "live_rsi": 61.0,
        "live_pdi": 31.0, "live_ndi": 4.0,
    }


def _redis_get_side_effect(key):
    if key == "kite:access_token":
        return "fake-token"
    if key == cfg.REDIS_EQUITY_TOKENS_KEY:
        return '{"RELIANCE": 12345}'
    if key == cfg.REDIS_DAILY_ATR_KEY:
        return '{"RELIANCE": 40.0}'
    if key == cfg.REDIS_OPTION_TOKENS_KEY:
        return None
    return None


def test_run_heartbeat_called_exactly_once_per_fired_signal():
    df = _make_df()
    result = _fired_ce_result()

    with (
        patch.object(stock_main.calendar_nse, "is_trading_day", return_value=True),
        patch.object(stock_main.calendar_nse, "in_eval_window_for", return_value=True),
        patch.object(stock_main.state, "redis_get", side_effect=_redis_get_side_effect),
        patch.object(stock_main, "get_live_quotes_batch", return_value={}),
        patch.object(cfg, "STOCKS", [_STOCK]),
        patch.object(stock_main, "_fetch_and_evaluate", return_value=(_STOCK, result, df)),
        patch.object(stock_main, "_is_duplicate", return_value=False),
        patch.object(stock_main, "_get_atm_option", return_value={
            "tradingsymbol": "RELIANCE26JUL1400CE", "strike": 1400,
            "ltp": 40.0, "expiry": "2026-07-30", "lot_size": 250,
            "fetch_time": "10:10:00 IST", "rolled_forward": False,
        }),
        patch.object(stock_main, "_load_dashboard", return_value=stock_main._empty_dashboard()),
        patch.object(stock_main, "_commit_dashboard") as mock_commit,
        patch.object(stock_main, "write_executor_intent"),
        patch.object(stock_main.tracker_bridge, "write_tracker_intent"),
        patch.object(stock_main.notifier, "send_signal"),
        patch.object(stock_main.position_tracker, "run_heartbeat") as mock_heartbeat,
    ):
        stock_main.main()

    mock_heartbeat.assert_called_once()
    mock_commit.assert_called_once()


def test_run_heartbeat_not_called_when_no_signal_fires():
    df = _make_df()
    result = _fired_ce_result()
    result["ce"]["signal"] = False  # no direction fires this run

    with (
        patch.object(stock_main.calendar_nse, "is_trading_day", return_value=True),
        patch.object(stock_main.calendar_nse, "in_eval_window_for", return_value=True),
        patch.object(stock_main.state, "redis_get", side_effect=_redis_get_side_effect),
        patch.object(stock_main, "get_live_quotes_batch", return_value={}),
        patch.object(cfg, "STOCKS", [_STOCK]),
        patch.object(stock_main, "_fetch_and_evaluate", return_value=(_STOCK, result, df)),
        patch.object(stock_main, "_load_dashboard", return_value=stock_main._empty_dashboard()),
        patch.object(stock_main, "_commit_dashboard"),
        patch.object(stock_main.position_tracker, "run_heartbeat") as mock_heartbeat,
    ):
        stock_main.main()

    mock_heartbeat.assert_not_called()
