"""
Unit test: tradingsymbol must be plain text (no markdown wrapping)
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


def test_tradingsymbol_plain_text_pe():
    """PE signal: tradingsymbol must be plain text (no code-block wrapping)."""
    _requests_stub.post.reset_mock()
    symbol = "BANKNIFTY26JUN55400PE"

    notifier.send_signal("BANKNIFTY", "PE", _make_result(symbol))

    call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_buy_field(call_kwargs["json"])

    assert buy_field["value"] == symbol, (
        f"Expected plain-text symbol, got: {buy_field['value']!r}"
    )
    print(f"✅ PE: tradingsymbol '{symbol}' is plain text")


def test_tradingsymbol_plain_text_ce():
    """CE signal: tradingsymbol must also be plain text (no code-block wrapping)."""
    _requests_stub.post.reset_mock()
    symbol = "BANKNIFTY26JUN55600CE"
    result = _make_result(symbol)
    result["atm_data"]["strike"] = 55600
    result["atm_data"]["tradingsymbol"] = symbol

    notifier.send_signal("BANKNIFTY", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_buy_field(call_kwargs["json"])

    assert buy_field["value"] == symbol, (
        f"Expected plain-text symbol, got: {buy_field['value']!r}"
    )
    print(f"✅ CE: tradingsymbol '{symbol}' is plain text")


def test_no_markdown_wrapping():
    """Confirm neither code-block nor bold-inline-code wrapping is present."""
    _requests_stub.post.reset_mock()
    symbol = "NIFTY26JUN24450CE"
    result = _make_result(symbol)
    result["atm_data"]["tradingsymbol"] = symbol

    notifier.send_signal("NIFTY", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    buy_field = _get_buy_field(call_kwargs["json"])

    assert not buy_field["value"].startswith(f"**`{symbol}`**"), (
        "Old bold-inline-code pattern still present"
    )
    assert not buy_field["value"].startswith("```"), (
        "Code-block pattern still present — breaks Android copy"
    )
    print("✅ No markdown wrapping — plain text confirmed")


def test_strike_and_expiry_line_still_present():
    """Strike and expiry must appear in the new 'Contract' field, not in 'Buy this option'."""
    _requests_stub.post.reset_mock()
    symbol = "BANKNIFTY26JUN55400PE"

    notifier.send_signal("BANKNIFTY", "PE", _make_result(symbol))

    call_kwargs = _requests_stub.post.call_args[1]
    embed = call_kwargs["json"]["embeds"][0]
    contract_field = next(f for f in embed["fields"] if f["name"] == "Contract")

    assert "55400" in contract_field["value"], "Strike missing from Contract field"
    assert "2026-06-30" in contract_field["value"], "Expiry missing from Contract field"
    print("✅ Strike and expiry found in Contract field")


# ── Sector conviction tag tests ───────────────────────────────────────────────

HIGH_CONVICTION_COLOR = 0x3498DB
LOW_CONVICTION_COLOR  = 0xE74C3C
CE_COLOR = 0x00E5A0
PE_COLOR = 0xF87171


def _make_stock_result(symbol: str = "RELIANCE26JUN1500CE", conviction: str | None = None) -> dict:
    r = {
        "atm_data": {
            "tradingsymbol": symbol,
            "strike": 1500,
            "expiry": "2026-06-30",
            "fetch_time": "11:05:00 IST",
        },
        "atm_ltp": 45.50,
        "opt_target": 55.20,
        "opt_sl": 38.00,
        "spot_ltp": 1502.0,
        "spot_tgt": 1540.0,
        "spot_sl": 1480.0,
        "futures_price": 1502.0,
        "fut_spot_spread": 0.0,
        "vwap": 1498.0,
        "rsi": 58.4,
        "pdi": 28.1,
        "ndi": 18.3,
        "conviction": "MED",
        "rr": 1.5,
        "candle_time": "11:00 IST",
        "c1": True, "c2": True, "c3": True, "c4": True,
        "asset_class": "STOCK",
        "delta_used": 0.50,
        "delta_fallback": False,
    }
    if conviction is not None:
        r["sector_conviction"] = conviction
    return r


def test_sector_conviction_high_produces_blue_color():
    """HIGH conviction → embed color must be 0x3498DB (blue)."""
    _requests_stub.post.reset_mock()
    result = _make_stock_result(conviction="HIGH")

    notifier.send_signal("RELIANCE", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    embed = call_kwargs["json"]["embeds"][0]
    assert embed["color"] == HIGH_CONVICTION_COLOR, (
        f"Expected blue ({HIGH_CONVICTION_COLOR:#x}), got {embed['color']:#x}"
    )
    print("✅ HIGH conviction → blue color")


def test_sector_conviction_high_field_text():
    """HIGH conviction → 'Sector Signal' field with correct text."""
    _requests_stub.post.reset_mock()
    result = _make_stock_result(conviction="HIGH")

    notifier.send_signal("RELIANCE", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    fields = call_kwargs["json"]["embeds"][0]["fields"]
    sector_field = next((f for f in fields if f["name"] == "Sector Signal"), None)
    assert sector_field is not None, "Sector Signal field missing for HIGH conviction"
    assert sector_field["value"] == "High Conviction with Sector Performance", (
        f"Unexpected field text: {sector_field['value']!r}"
    )
    print("✅ HIGH conviction → correct field text")


def test_sector_conviction_low_produces_red_color():
    """LOW conviction → embed color must be 0xE74C3C (red)."""
    _requests_stub.post.reset_mock()
    result = _make_stock_result(conviction="LOW")

    notifier.send_signal("RELIANCE", "PE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    embed = call_kwargs["json"]["embeds"][0]
    assert embed["color"] == LOW_CONVICTION_COLOR, (
        f"Expected red ({LOW_CONVICTION_COLOR:#x}), got {embed['color']:#x}"
    )
    print("✅ LOW conviction → red color")


def test_sector_conviction_low_field_text():
    """LOW conviction → 'Sector Signal' field with correct text."""
    _requests_stub.post.reset_mock()
    result = _make_stock_result(conviction="LOW")

    notifier.send_signal("RELIANCE", "PE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    fields = call_kwargs["json"]["embeds"][0]["fields"]
    sector_field = next((f for f in fields if f["name"] == "Sector Signal"), None)
    assert sector_field is not None, "Sector Signal field missing for LOW conviction"
    assert sector_field["value"] == "Low Conviction with Sector Performance", (
        f"Unexpected field text: {sector_field['value']!r}"
    )
    print("✅ LOW conviction → correct field text")


def test_sector_conviction_none_no_field():
    """None conviction → no 'Sector Signal' field present."""
    _requests_stub.post.reset_mock()
    result = _make_stock_result()  # no sector_conviction key

    notifier.send_signal("RELIANCE", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    fields = call_kwargs["json"]["embeds"][0]["fields"]
    sector_field = next((f for f in fields if f["name"] == "Sector Signal"), None)
    assert sector_field is None, "Unexpected Sector Signal field for None conviction"
    print("✅ None conviction → no Sector Signal field")


def test_sector_conviction_none_color_unchanged_ce():
    """None conviction CE → color is standard CE green."""
    _requests_stub.post.reset_mock()
    result = _make_stock_result()

    notifier.send_signal("RELIANCE", "CE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    embed = call_kwargs["json"]["embeds"][0]
    assert embed["color"] == CE_COLOR, (
        f"Expected CE green ({CE_COLOR:#x}), got {embed['color']:#x}"
    )
    print("✅ None conviction CE → standard green color")


def test_sector_conviction_none_color_unchanged_pe():
    """None conviction PE → color is standard PE red."""
    _requests_stub.post.reset_mock()
    result = _make_stock_result(symbol="RELIANCE26JUN1500PE")

    notifier.send_signal("RELIANCE", "PE", result)

    call_kwargs = _requests_stub.post.call_args[1]
    embed = call_kwargs["json"]["embeds"][0]
    assert embed["color"] == PE_COLOR, (
        f"Expected PE red ({PE_COLOR:#x}), got {embed['color']:#x}"
    )
    print("✅ None conviction PE → standard red color")
