"""Tests for write_executor_intent's static vs. dynamic-stock payload handling."""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

from src import executor_bridge


def _base_signal_result(direction="ce"):
    return {
        "ce": {"signal": direction == "ce"},
        "pe": {"signal": direction == "pe"},
        "futures_price": 100.0,
        "prev_candle_low": 95.0,
        "prev_candle_high": 105.0,
        "spot_ltp": 100.0,
        "atm_strike": 100,
        "atm_data": {"tradingsymbol": "FOO24AUG100CE"},
        "vwap": 99.0,
        "rsi": 60.0,
        "pdi": 30.0,
        "ndi": 15.0,
        "c5": True,
    }


def _patched(**overrides):
    """Context manager stack patching redis env + calls; returns captured intent holder."""
    captured = {}

    def fake_setex(key, ttl, value):
        captured["key"] = key
        captured["ttl"] = ttl
        captured["value"] = json.loads(value)
        return True

    patches = [
        patch.object(executor_bridge, "REDIS_URL", "https://fake-redis"),
        patch.object(executor_bridge, "REDIS_TOKEN", "fake-token"),
        patch.object(executor_bridge, "_redis_get", return_value=None),
        patch.object(executor_bridge, "_redis_setex", side_effect=fake_setex),
    ]
    return patches, captured


def test_static_stock_unchanged_behavior():
    patches, captured = _patched()
    stock_cfg = {"name": "RELIANCE", "strike_step": 10}
    for p in patches:
        p.start()
    try:
        result = executor_bridge.write_executor_intent(_base_signal_result("ce"), stock_cfg)
    finally:
        for p in patches:
            p.stop()

    assert result is True
    intent = captured["value"]
    assert intent["is_dynamic"] is False
    for extra_key in ("lot_size", "equity_token", "fno_exchange", "direction_restriction"):
        assert extra_key not in intent


def test_dynamic_stock_complete_metadata_writes_enriched_intent():
    patches, captured = _patched()
    stock_cfg = {
        "name": "ADANIENT",
        "strike_step": 20,
        "is_dynamic": True,
        "lot_size": 250,
        "equity_token": 12345,
        "fno_exchange": "NFO",
        "direction_restriction": "CE",
    }
    for p in patches:
        p.start()
    try:
        result = executor_bridge.write_executor_intent(_base_signal_result("ce"), stock_cfg)
    finally:
        for p in patches:
            p.stop()

    assert result is True
    intent = captured["value"]
    assert intent["is_dynamic"] is True
    assert intent["lot_size"] == 250
    assert intent["equity_token"] == 12345
    assert intent["fno_exchange"] == "NFO"
    assert intent["strike_step"] == 20
    assert intent["direction_restriction"] == "CE"


def test_dynamic_stock_missing_metadata_aborts_write():
    patches, captured = _patched()
    stock_cfg = {
        "name": "ADANIENT",
        "strike_step": 20,
        "is_dynamic": True,
        "lot_size": 250,
        # equity_token missing
        "fno_exchange": "NFO",
        "direction_restriction": "CE",
    }
    for p in patches:
        p.start()
    try:
        result = executor_bridge.write_executor_intent(_base_signal_result("ce"), stock_cfg)
    finally:
        for p in patches:
            p.stop()

    assert result is False
    assert "value" not in captured
