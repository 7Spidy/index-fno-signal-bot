"""
Unit test: tradingsymbol must be in a triple-backtick code block
in the Discord embed 'Buy this option' field.
"""
import os
import sys
import types
import unittest.mock as mock

# ── Stub requests so no real HTTP call is made ──────────────────
_requests_stub = types.ModuleType("requests")
_mock_response = mock.MagicMock()
_mock_response.status_code = 204
_requests_stub.post = mock.MagicMock(return_value=_mock_response)
sys.modules["requests"] = _requests_stub

# ── Set required env var ────────────────────────────────────────
os.environ["DISCORD_WEBHOOK_URL"] = "https://example.com/fake-webhook"

# ── Import notifier AFTER stubbing ──────────────────────────────
from src import notifier  # noqa: E402


def _make_result(symbol: str = "BANKNIFTY26JUN55400PE") -> dict:
    return {
        "atm_data": {
            "tradingsymbol": symbol,
            "strike": 55400,
            "expiry": "2026-06-30",
            "fetch_time": "14:35:46 IST",
        },
        "atm_ltp": 976.10,
        "opt_target": 1050.31,
        "opt_sl": 926.62,
        "spot_ltp": 55399.0,
        "spot_tgt": 55236.0,
        "spot_sl": 55483.4,
        "futures_price": 55414.0,
        "fut_spot_spread": 15.0,
        "vwap": 55426.0,
        "rsi": 42.9,
        "pdi": 12.1,
        "ndi": 30.8,
        "conviction": "Strong",
        "rr": 1.5,
        "candle_time": "14:30 IST",
        "c1": True, "c2": True, "c3": True, "c4": True,
    }


def _get_buy_field(payload: dict) -> dict:
    embed = payload["embeds"][0]
    return next(f for f in embed["fields"] if f["name"] == "Buy this option")


def test_tradingsymbol_in_code_block_pe():
    """PE signal: tradingsymbol must be wrapped in a triple-backtick code block."""
    _requests_stub.post.reset_mock()
    symbol = "BANKNIFTY26JUN55400PE"

    notifier.send_signal("BANKNIFTY", "PE", _make_result(symbol))

    call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_buy_field(call_kwargs["json"])

    expected_prefix = f"```\n{symbol}\n```"
    assert buy_field["value"].startswith(expected_prefix), (
        f"Expected field to start with code block, got: {buy_field['value']!r}"
    )
    print(f"✅ PE: tradingsymbol '{symbol}' is in a code block")


def test_tradingsymbol_in_code_block_ce():
    """CE signal: tradingsymbol must also be wrapped in a triple-backtick code block."""
    _requests_stub.post.reset_mock()
    symbol = "BANKNIFTY26JUN55600CE"
    result = _make_result(symbol)
    result["atm_data"]["strike"] = 55600
    result["atm_data"]["tradingsymbol"] = symbol

    notifier.send_signal("BANKNIFTY", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_buy_field(call_kwargs["json"])

    expected_prefix = f"```\n{symbol}\n```"
    assert buy_field["value"].startswith(expected_prefix), (
        f"Expected field to start with code block, got: {buy_field['value']!r}"
    )
    print(f"✅ CE: tradingsymbol '{symbol}' is in a code block")


def test_no_bold_inline_code_pattern():
    """Confirm the OLD bold-inline-code pattern is gone."""
    _requests_stub.post.reset_mock()
    symbol = "NIFTY26JUN24450CE"
    result = _make_result(symbol)
    result["atm_data"]["tradingsymbol"] = symbol

    notifier.send_signal("NIFTY", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_buy_field(call_kwargs["json"])

    assert not buy_field["value"].startswith(f"**`{symbol}`**"), (
        "Old bold-inline-code pattern still present — change not applied"
    )
    print("✅ Old **`symbol`** pattern is absent")


def test_strike_and_expiry_line_still_present():
    """The strike / expiry line below the code block must be preserved."""
    _requests_stub.post.reset_mock()
    symbol = "BANKNIFTY26JUN55400PE"

    notifier.send_signal("BANKNIFTY", "PE", _make_result(symbol))

    call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_buy_field(call_kwargs["json"])

    assert "55400" in buy_field["value"], "Strike missing from field value"
    assert "2026-06-30" in buy_field["value"], "Expiry missing from field value"
    print("✅ Strike and expiry line preserved below code block")
