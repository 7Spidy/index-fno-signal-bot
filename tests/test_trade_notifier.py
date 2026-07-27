"""
Regression test: PE unrealized P&L sign inversion.

Options are always bought long (CE or PE) — premium rising is always
profit, regardless of option type. `_build_consolidated_embed()` must
use `(ltp - entry) * lot_size` for both directions, not branch on
direction (that was the bug: PE used `(entry - ltp)`, inverting the sign).
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
_requests_stub.patch = mock.MagicMock(return_value=_mock_response)
sys.modules["requests"] = _requests_stub

# ── Set required env var ────────────────────────────────────────
os.environ["DISCORD_TRADE_TRACKER_WEBHOOK_URL"] = "https://example.com/fake-webhook"

# ── Import trade_notifier AFTER stubbing ────────────────────────
from src import trade_notifier  # noqa: E402


def _open_position(direction: str, entry: float, ltp: float, lot_size: int = 10) -> dict:
    return {
        "instrument": "ASIANPAINT",
        "tradingsymbol": f"ASIANPAINT26JUL2640{direction}",
        "direction": direction,
        "entry_price": entry,
        "current_ltp": ltp,
        "sl_ladder_stage": entry,
        "lot_size": lot_size,
    }


def _unreal_field_value(pos: dict) -> str:
    embed = trade_notifier._build_consolidated_embed([pos], [], "2026-07-13")
    return embed["fields"][0]["value"]


def test_pe_premium_up_is_profit():
    """PE with ltp > entry (premium up) is a real profit — must show positive."""
    pos = _open_position("PE", entry=47.50, ltp=52.00, lot_size=10)
    value = _unreal_field_value(pos)
    expected = (52.00 - 47.50) * 10
    assert expected > 0
    assert f"+₹{expected:.0f}" in value, f"Expected positive unrealized in: {value!r}"


def test_pe_premium_down_is_loss():
    """PE with ltp < entry (premium down) is a real loss — must show negative.

    Regression case: ASIANPAINT26JUL2640PE entry 47.50, LTP 44.40 — a real
    loss that the buggy `(entry - ltp)` branch displayed as "+₹775".
    """
    pos = _open_position("PE", entry=47.50, ltp=44.40, lot_size=250)
    value = _unreal_field_value(pos)
    expected = (44.40 - 47.50) * 250
    assert expected < 0
    assert f"₹{expected:.0f}" in value
    assert "+₹775" not in value, "Old inverted-sign bug output must not reappear"


def test_ce_premium_up_is_profit():
    """CE with ltp > entry (premium up) is a real profit — must show positive."""
    pos = _open_position("CE", entry=100.00, ltp=110.00, lot_size=10)
    value = _unreal_field_value(pos)
    expected = (110.00 - 100.00) * 10
    assert expected > 0
    assert f"+₹{expected:.0f}" in value, f"Expected positive unrealized in: {value!r}"


def test_ce_premium_down_is_loss():
    """CE with ltp < entry (premium down) is a real loss — must show negative."""
    pos = _open_position("CE", entry=100.00, ltp=90.00, lot_size=10)
    value = _unreal_field_value(pos)
    expected = (90.00 - 100.00) * 10
    assert expected < 0
    assert f"₹{expected:.0f}" in value


def test_ce_and_pe_use_identical_formula():
    """Same entry/ltp/lot_size magnitude → CE and PE unrealized P&L match in sign and value."""
    ce_pos = _open_position("CE", entry=50.0, ltp=60.0, lot_size=10)
    pe_pos = _open_position("PE", entry=50.0, ltp=60.0, lot_size=10)

    ce_value = _unreal_field_value(ce_pos)
    pe_value = _unreal_field_value(pe_pos)

    expected = (60.0 - 50.0) * 10
    assert f"+₹{expected:.0f}" in ce_value
    assert f"+₹{expected:.0f}" in pe_value


# ── Ladder progress stages (current + next) ───────────────────────

def test_stages_below_half():
    result = trade_notifier._ladder_progress_stages(entry=100, T=10, ltp=102)
    assert result == (None, 0.5)


def test_stages_after_one_point_one():
    result = trade_notifier._ladder_progress_stages(entry=100, T=10, ltp=112)
    current_stage, next_stage = result
    assert current_stage == 1.2
    assert next_stage == 1.3


def test_stages_none_when_T_missing():
    result = trade_notifier._ladder_progress_stages(entry=100, T=None, ltp=102)
    assert result is None

    pos = _open_position("CE", entry=100.0, ltp=102.0)
    pos.pop("target_t", None)
    value = _unreal_field_value(pos)
    assert "T =" not in value


def test_open_field_shows_current_and_next_threshold():
    pos = _open_position("CE", entry=100.0, ltp=112.0)
    pos["target_t"] = 10
    value = _unreal_field_value(pos)
    assert "1.2T = ₹112.00 (current)" in value
    assert "1.3T = ₹113.00 (next)" in value


# ── Exit result + trailing-SL label ────────────────────────────────

def test_exit_label_profit_with_trailed_sl():
    rec = {"pnl_net": 150.0, "initial_sl": 20.0, "exit_sl_stage": 35.0}
    assert trade_notifier._result_and_trail_label(rec) == "PROFIT · SL trailed"


def test_exit_label_loss_at_initial_sl():
    rec = {"pnl_net": -80.0, "initial_sl": 20.0, "exit_sl_stage": 20.0}
    assert trade_notifier._result_and_trail_label(rec) == "LOSS · exited at initial SL"


def test_exit_label_no_initial_sl_recorded():
    rec = {"pnl_net": 42.0, "initial_sl": None, "exit_sl_stage": None}
    assert trade_notifier._result_and_trail_label(rec) == "PROFIT"
