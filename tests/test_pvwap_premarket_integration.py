"""Integration-style test for src/pvwap_signals.run_premarket() — mocks Kite
historical-data responses and exercises the full pre-market -> bias ->
Redis-cache -> Discord-payload pipeline, including the 3x-retry ->
NEUTRAL fallback path."""
import json
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src import pvwap_signals


def _df(highs, lows, closes, freq):
    n = len(highs)
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq=freq),
        "open":   closes,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": np.ones(n) * 1000,
    })


def _trap_df_1h():
    # Two nearby swing lows (~23980-23985) cluster into one support zone;
    # last close is 24120. window=2, min_touches=2, tolerance=0.5% (patched
    # in the tests below to keep the fixture small).
    highs  = [24100, 24120, 24110, 24130, 24140, 24125, 24135, 24150, 24160]
    lows   = [24050, 24060, 23980, 24055, 24065, 23985, 24070, 24075, 24080]
    closes = [24075, 24090, 24045, 24090, 24100, 24055, 24100, 24110, 24120]
    return _df(highs, lows, closes, "1h")


def _fib_df_15m():
    highs  = [100, 101, 102, 110, 102, 101, 100, 99, 98]
    lows   = [90, 91, 80, 91, 92, 79, 91, 92, 93]
    closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    return _df(highs, lows, closes, "15min")


@pytest.fixture(autouse=True)
def _small_zone_params(monkeypatch):
    monkeypatch.setattr(pvwap_signals.config, "PVWAP_FRACTAL_WINDOW", 2)
    monkeypatch.setattr(pvwap_signals.config, "PVWAP_MIN_TOUCHES", 2)
    monkeypatch.setattr(pvwap_signals.config, "PVWAP_TOUCH_TOLERANCE_PCT", 0.5)
    monkeypatch.setattr(pvwap_signals, "_PREMARKET_RETRY_BACKOFF_SECONDS", 0)


def _mock_tokens_redis_get(key):
    if key == "kite:instrument_tokens":
        return json.dumps({"NIFTY": {"token": 12345, "tradingsymbol": "NIFTY26JULFUT"}})
    return None


class TestRunPremarketFullPipeline:
    def test_computes_trap_bias_caches_and_alerts(self):
        with patch("src.pvwap_signals.state.redis_get", side_effect=_mock_tokens_redis_get), \
             patch("src.pvwap_signals.state.redis_set", return_value=True) as mock_set, \
             patch("src.kite_client.get_kite"), \
             patch("src.kite_client.fetch_ohlcv_multi") as mock_fetch, \
             patch("src.kite_client.get_spot_ltp", return_value=24000.0), \
             patch("src.notifier.send_pvwap_bias") as mock_notify:

            def _fetch_side_effect(token, interval, lookback_days):
                assert token == 12345
                return _trap_df_1h() if interval == "60minute" else _fib_df_15m()

            mock_fetch.side_effect = _fetch_side_effect

            result = pvwap_signals.run_premarket()

        assert result["bias"] == "CE"
        assert result["rationale"] == "trap"

        set_keys = [c.args[0] for c in mock_set.call_args_list]
        assert any(k.startswith("pvwap:bias:") for k in set_keys)
        assert any(k.startswith("pvwap:zones:") for k in set_keys)

        bias_call = next(c for c in mock_set.call_args_list if c.args[0].startswith("pvwap:bias:"))
        cached = json.loads(bias_call.args[1])
        assert cached["bias"] == "CE"

        mock_notify.assert_called_once()
        notified_bias = mock_notify.call_args.args[1]
        assert notified_bias["bias"] == "CE"

    def test_retry_exhausted_falls_back_to_neutral(self):
        with patch("src.pvwap_signals.state.redis_get", side_effect=_mock_tokens_redis_get), \
             patch("src.pvwap_signals.state.redis_set", return_value=True) as mock_set, \
             patch("src.kite_client.get_kite"), \
             patch("src.kite_client.fetch_ohlcv_multi", side_effect=RuntimeError("Kite API down")) as mock_fetch, \
             patch("src.notifier.send_pvwap_bias") as mock_notify:

            result = pvwap_signals.run_premarket()

        assert result["bias"] == "NEUTRAL"
        assert result["rationale"] == "premarket_fetch_failed"
        # 3 attempts for the 1h fetch before giving up on it
        assert mock_fetch.call_count >= pvwap_signals._PREMARKET_FETCH_RETRIES

        bias_call = next(c for c in mock_set.call_args_list if c.args[0].startswith("pvwap:bias:"))
        cached = json.loads(bias_call.args[1])
        assert cached["rationale"] == "premarket_fetch_failed"

        mock_notify.assert_called_once()
        notified_bias = mock_notify.call_args.args[1]
        assert notified_bias["bias"] == "NEUTRAL"

    def test_missing_instrument_token_falls_back_to_neutral_without_fetch(self):
        with patch("src.pvwap_signals.state.redis_get", return_value=None), \
             patch("src.pvwap_signals.state.redis_set", return_value=True), \
             patch("src.kite_client.fetch_ohlcv_multi") as mock_fetch, \
             patch("src.notifier.send_pvwap_bias") as mock_notify:

            result = pvwap_signals.run_premarket()

        assert result["bias"] == "NEUTRAL"
        assert result["rationale"] == "premarket_fetch_failed"
        mock_fetch.assert_not_called()
        mock_notify.assert_called_once()
