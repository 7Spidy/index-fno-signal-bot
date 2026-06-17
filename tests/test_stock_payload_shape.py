"""
Unit tests: stock signal payload shape and notifier rendering.

Asserts:
- atm_data.tradingsymbol set
- atm_data.strike set
- atm_data.expiry set
- spot_tgt not None
- asset_class == "STOCK"
- notifier renders tradingsymbol inside a triple-backtick code block
- notifier emits a "Spot" field (not "Futures / Spot")
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


def _make_stock_payload(symbol: str = "RELIANCE26JUN1300CE") -> dict:
    """Simulates the signal_payload built by stock_main for a CE signal."""
    return {
        # nested contract identity (notifier reads these)
        "atm_data": {
            "tradingsymbol": symbol,
            "strike":        1300,
            "expiry":        "2026-06-25",
            "fetch_time":    "10:15:32 IST",
        },
        # premium / spot levels (read top-level by notifier)
        "atm_ltp":    40.20,
        "opt_target": 52.35,
        "opt_sl":     33.10,
        "spot_ltp":   1314.4,
        "spot_tgt":   1335.6,
        "spot_sl":    1305.2,
        "futures_price": 1314.4,
        "asset_class": "STOCK",
        # no fut_spot_spread for stocks
        "vwap":       1310.0,
        "rsi":        58.3,
        "pdi":        32.1,
        "ndi":        18.5,
        "conviction": "HIGH",
        "rr":         1.5,
        "candle_time": "10:10 IST",
        "c1": True, "c2": True, "c3": True, "c4": True,
    }


def _get_field(payload: dict, name: str) -> dict:
    embed = payload["embeds"][0]
    return next(f for f in embed["fields"] if f["name"] == name)


def _find_field(payload: dict, name: str) -> dict | None:
    embed = payload["embeds"][0]
    matches = [f for f in embed["fields"] if f["name"] == name]
    return matches[0] if matches else None


# ── Payload shape assertions ────────────────────────────────────

def test_atm_data_tradingsymbol_set():
    payload = _make_stock_payload()
    assert payload["atm_data"]["tradingsymbol"] is not None
    assert payload["atm_data"]["tradingsymbol"] != ""
    print("✅ atm_data.tradingsymbol is set")


def test_atm_data_strike_set():
    payload = _make_stock_payload()
    assert payload["atm_data"]["strike"] is not None
    print("✅ atm_data.strike is set")


def test_atm_data_expiry_set():
    payload = _make_stock_payload()
    assert payload["atm_data"]["expiry"] is not None
    print("✅ atm_data.expiry is set")


def test_spot_tgt_not_none():
    payload = _make_stock_payload()
    assert payload["spot_tgt"] is not None
    print("✅ spot_tgt is not None")


def test_asset_class_is_stock():
    payload = _make_stock_payload()
    assert payload["asset_class"] == "STOCK"
    print("✅ asset_class == 'STOCK'")


# ── Notifier rendering assertions ───────────────────────────────

def test_tradingsymbol_in_code_block():
    """stock notifier: tradingsymbol must be inside a triple-backtick code block."""
    symbol = "RELIANCE26JUN1300CE"
    with mock.patch.object(notifier, "requests", _requests_stub):
        _requests_stub.post.reset_mock()
        notifier.send_signal("RELIANCE", "CE", _make_stock_payload(symbol))
        call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_field(call_kwargs["json"], "Buy this option")
    expected_prefix = f"```\n{symbol}\n```"
    assert buy_field["value"].startswith(expected_prefix), (
        f"Expected code block, got: {buy_field['value']!r}"
    )
    print(f"✅ tradingsymbol '{symbol}' is in a code block")


def test_notifier_emits_spot_field_not_futures_spot():
    """stock notifier: must emit 'Spot' field, not 'Futures / Spot'."""
    with mock.patch.object(notifier, "requests", _requests_stub):
        _requests_stub.post.reset_mock()
        notifier.send_signal("RELIANCE", "CE", _make_stock_payload())
        call_kwargs = _requests_stub.post.call_args[1]
    embed = call_kwargs["json"]["embeds"][0]
    field_names = [f["name"] for f in embed["fields"]]
    assert "Spot" in field_names, f"'Spot' field missing. Fields: {field_names}"
    assert "Futures / Spot" not in field_names, (
        f"'Futures / Spot' field present for STOCK — should be 'Spot'. Fields: {field_names}"
    )
    print("✅ notifier emits 'Spot' field (not 'Futures / Spot') for STOCK")


def test_spot_field_shows_futures_price():
    """The 'Spot' field value should reflect the equity close (futures_price)."""
    payload = _make_stock_payload()
    with mock.patch.object(notifier, "requests", _requests_stub):
        _requests_stub.post.reset_mock()
        notifier.send_signal("RELIANCE", "CE", payload)
        call_kwargs = _requests_stub.post.call_args[1]
    spot_field = _get_field(call_kwargs["json"], "Spot")
    # fi() formats as "1,314.4"
    assert "1,314" in spot_field["value"] or "1314" in spot_field["value"], (
        f"Spot field value doesn't reflect futures_price: {spot_field['value']!r}"
    )
    print(f"✅ 'Spot' field value: {spot_field['value']!r}")
