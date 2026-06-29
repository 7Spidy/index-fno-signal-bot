"""
Unit tests for stock_kite_client._get_nearest_monthly_expiry() rollover logic.
Mocks the NFO instruments dump — no real API calls.
"""

from __future__ import annotations

import datetime as real_dt
from unittest.mock import patch

import pytest
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_instruments(name: str, expiries: list[real_dt.date]) -> list[dict]:
    """CE + PE rows for every expiry."""
    rows = []
    for exp in expiries:
        for itype in ("CE", "PE"):
            rows.append({
                "name":            name,
                "instrument_type": itype,
                "expiry":          exp,
                "instrument_token": 1,
                "tradingsymbol":   f"{name}{exp.isoformat()}{itype}",
                "strike":          2000,
                "lot_size":        500,
            })
    return rows


def _call(name: str, today: real_dt.date, instruments: list[dict],
          is_trading_day_fn=None):
    """Invoke _get_nearest_monthly_expiry() with controlled dependencies."""
    if is_trading_day_fn is None:
        def is_trading_day_fn(d: real_dt.date) -> bool:
            return d.weekday() < 5

    with (
        patch("src.calendar_nse.is_trading_day", side_effect=is_trading_day_fn),
        patch("src.stock_kite_client.date") as mock_date,
    ):
        # date.today() must return our fixed `today`
        mock_date.today.return_value = today
        # date.fromisoformat and arithmetic still need the real class
        mock_date.side_effect = lambda *a, **kw: real_dt.date(*a, **kw)

        from src.stock_kite_client import _get_nearest_monthly_expiry
        return _get_nearest_monthly_expiry(name, instruments)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_rollover_when_nearest_is_today():
    """Nearest expiry == today → roll to next monthly."""
    today    = real_dt.date(2026, 6, 25)
    near_exp = real_dt.date(2026, 6, 25)
    far_exp  = real_dt.date(2026, 7, 30)
    instruments = _make_instruments("RELIANCE", [near_exp, far_exp])

    exp, rolled = _call("RELIANCE", today, instruments)

    assert exp == far_exp
    assert rolled is True


def test_rollover_when_nearest_is_next_trading_day():
    """Nearest expiry == next trading day (Mon after Fri) → roll."""
    today    = real_dt.date(2026, 6, 26)  # Friday
    near_exp = real_dt.date(2026, 6, 29)  # Monday = next trading day
    far_exp  = real_dt.date(2026, 7, 30)
    instruments = _make_instruments("RELIANCE", [near_exp, far_exp])

    def is_td(d: real_dt.date) -> bool:
        return d.weekday() < 5

    exp, rolled = _call("RELIANCE", today, instruments, is_trading_day_fn=is_td)

    assert exp == far_exp
    assert rolled is True


def test_rollover_next_trading_day_skips_holiday():
    """Next trading day computation skips weekends + holidays correctly."""
    today     = real_dt.date(2026, 5, 28)  # Thursday
    near_exp  = real_dt.date(2026, 6, 1)   # Monday = next trading day (Fri=holiday)
    far_exp   = real_dt.date(2026, 6, 26)
    instruments = _make_instruments("RELIANCE", [near_exp, far_exp])

    holiday = real_dt.date(2026, 5, 29)

    def is_td(d: real_dt.date) -> bool:
        return d.weekday() < 5 and d != holiday

    exp, rolled = _call("RELIANCE", today, instruments, is_trading_day_fn=is_td)

    assert exp == far_exp
    assert rolled is True


def test_no_rollover_when_expiry_two_or_more_days_out():
    """Nearest expiry 2+ days away → no rollover."""
    today    = real_dt.date(2026, 6, 23)
    near_exp = real_dt.date(2026, 6, 26)  # Thursday, 3 days out
    far_exp  = real_dt.date(2026, 7, 30)
    instruments = _make_instruments("RELIANCE", [near_exp, far_exp])

    exp, rolled = _call("RELIANCE", today, instruments)

    assert exp == near_exp
    assert rolled is False


def test_fallback_to_candidate_when_only_one_expiry(capfd):
    """Only one expiry available when rollover should apply → use it, warn, don't raise."""
    today    = real_dt.date(2026, 6, 25)
    near_exp = real_dt.date(2026, 6, 25)  # today, only option
    instruments = _make_instruments("RELIANCE", [near_exp])

    exp, rolled = _call("RELIANCE", today, instruments)

    assert exp == near_exp
    assert rolled is False
    assert "WARNING" in capfd.readouterr().out


def test_returns_none_when_no_expiries():
    """No matching instruments → (None, False)."""
    today = real_dt.date(2026, 6, 25)

    with (
        patch("src.calendar_nse.is_trading_day", return_value=True),
        patch("src.stock_kite_client.date") as mock_date,
    ):
        mock_date.today.return_value = today
        mock_date.side_effect = lambda *a, **kw: real_dt.date(*a, **kw)

        from src.stock_kite_client import _get_nearest_monthly_expiry
        exp, rolled = _get_nearest_monthly_expiry("UNKNOWN", [])

    assert exp is None
    assert rolled is False
