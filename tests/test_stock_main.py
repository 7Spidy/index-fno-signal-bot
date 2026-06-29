"""
Unit tests for stock_main._live_atm_fallback() rollover logic.
Verifies that rolled_forward propagates into the returned dict.
Mocks the live NFO dump — no real API calls.
"""

from __future__ import annotations

import datetime as real_dt
import types
from unittest.mock import MagicMock, patch

import pytest
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Stub kiteconnect before importing any src module.
import sys
_kc_stub = types.ModuleType("kiteconnect")
_kc_stub.KiteConnect = MagicMock
sys.modules.setdefault("kiteconnect", _kc_stub)

_dotenv_stub = types.ModuleType("dotenv")
_dotenv_stub.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", _dotenv_stub)


# ── Helpers ───────────────────────────────────────────────────────────────────

_NAME  = "RELIANCE"
_STEP  = 50
_SPOT  = 1400.0
_ATM   = 1400   # round(1400 / 50) * 50


def _make_nfo_row(expiry: real_dt.date, itype: str = "CE") -> dict:
    return {
        "name":             _NAME,
        "instrument_type":  itype,
        "expiry":           expiry,
        "strike":           _ATM,
        "instrument_token": 42,
        "tradingsymbol":    f"{_NAME}{expiry.isoformat()}{itype}",
        "lot_size":         500,
    }


def _run_fallback(
    today: real_dt.date,
    expiries: list[real_dt.date],
    is_trading_day_fn=None,
    direction: str = "CE",
):
    """Call _live_atm_fallback with fully mocked I/O."""
    if is_trading_day_fn is None:
        def is_trading_day_fn(d: real_dt.date) -> bool:
            return d.weekday() < 5

    instruments = []
    for exp in expiries:
        instruments.append(_make_nfo_row(exp, "CE"))
        instruments.append(_make_nfo_row(exp, "PE"))

    mock_kite = MagicMock()
    mock_kite.instruments.return_value = instruments

    ts_key = f"NFO:{_NAME}{expiries[-1].isoformat()}{direction}"
    mock_kite.ltp.return_value = {
        f"NFO:{_NAME}{exp.isoformat()}{direction}": {"last_price": 99.5}
        for exp in expiries
    }

    with (
        patch("src.stock_main.get_kite", return_value=mock_kite),
        patch("src.calendar_nse.is_trading_day", side_effect=is_trading_day_fn),
        patch("src.stock_main.date") as mock_date,
    ):
        mock_date.today.return_value = today
        # Arithmetic on real dates must still work (today + timedelta)
        mock_date.side_effect = lambda *a, **kw: real_dt.date(*a, **kw)

        from src.stock_main import _live_atm_fallback
        return _live_atm_fallback(_NAME, _SPOT, _STEP, direction)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_rollover_when_nearest_is_today():
    """Nearest expiry == today → roll to next expiry, rolled_forward=True."""
    today    = real_dt.date(2026, 6, 25)
    near_exp = real_dt.date(2026, 6, 25)
    far_exp  = real_dt.date(2026, 7, 30)

    result = _run_fallback(today, [near_exp, far_exp])

    assert result.get("rolled_forward") is True
    assert result.get("expiry") == far_exp


def test_rollover_when_nearest_is_next_trading_day():
    """Nearest expiry == next trading day → roll."""
    today    = real_dt.date(2026, 6, 26)  # Friday
    near_exp = real_dt.date(2026, 6, 29)  # Monday
    far_exp  = real_dt.date(2026, 7, 30)

    def is_td(d: real_dt.date) -> bool:
        return d.weekday() < 5

    result = _run_fallback(today, [near_exp, far_exp], is_trading_day_fn=is_td)

    assert result.get("rolled_forward") is True
    assert result.get("expiry") == far_exp


def test_rollover_next_trading_day_skips_holiday():
    """Next trading day computation correctly skips weekend + holiday."""
    today    = real_dt.date(2026, 5, 28)  # Thursday
    near_exp = real_dt.date(2026, 6, 1)   # Monday
    far_exp  = real_dt.date(2026, 6, 26)

    holiday = real_dt.date(2026, 5, 29)

    def is_td(d: real_dt.date) -> bool:
        return d.weekday() < 5 and d != holiday

    result = _run_fallback(today, [near_exp, far_exp], is_trading_day_fn=is_td)

    assert result.get("rolled_forward") is True
    assert result.get("expiry") == far_exp


def test_no_rollover_when_expiry_two_or_more_days_out():
    """2+ days until expiry → no rollover, rolled_forward=False."""
    today    = real_dt.date(2026, 6, 23)
    near_exp = real_dt.date(2026, 6, 26)  # 3 days away
    far_exp  = real_dt.date(2026, 7, 30)

    result = _run_fallback(today, [near_exp, far_exp])

    assert result.get("rolled_forward") is False
    assert result.get("expiry") == near_exp


def test_fallback_to_candidate_when_only_one_expiry(capfd):
    """Single expiry in dump when rollover should apply → use it, warn, don't raise."""
    today    = real_dt.date(2026, 6, 25)
    near_exp = real_dt.date(2026, 6, 25)  # today, only entry

    result = _run_fallback(today, [near_exp])

    assert result.get("rolled_forward") is False
    assert result.get("expiry") == near_exp
    assert "WARNING" in capfd.readouterr().out


def test_rolled_forward_false_in_normal_case():
    """rolled_forward must be False (not None or missing) when no rollover."""
    today    = real_dt.date(2026, 6, 23)
    near_exp = real_dt.date(2026, 6, 26)
    far_exp  = real_dt.date(2026, 7, 30)

    result = _run_fallback(today, [near_exp, far_exp])

    assert "rolled_forward" in result
    assert result["rolled_forward"] is False
