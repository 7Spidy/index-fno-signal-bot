"""
Unit tests for kite_client.get_nearest_expiry() rollover logic.
Mocks the live Kite instrument dump and datetime.now — no real API calls.
kiteconnect is installed, so no sys.modules stub is needed.
"""

from __future__ import annotations

import datetime as real_dt
from unittest.mock import MagicMock, patch

import pytest
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Ensure src.kite_client is importable before any test patches touch sys.modules.
import src.kite_client  # noqa: E402, F401


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_now(d: real_dt.date) -> real_dt.datetime:
    """Fixed 10:00 AM IST on the given date."""
    return real_dt.datetime(d.year, d.month, d.day, 10, 0, 0, tzinfo=IST)


def _make_instruments(name: str, expiries: list[real_dt.date]) -> list[dict]:
    """CE + PE rows for every expiry — mirrors the real NFO dump shape."""
    rows = []
    for exp in expiries:
        for itype in ("CE", "PE"):
            rows.append({
                "name":             name,
                "instrument_type":  itype,
                "expiry":           exp,
                "instrument_token": 1,
                "tradingsymbol":    f"{name}{exp.isoformat()}{itype}",
                "strike":           24000,
                "lot_size":         25,
            })
    return rows


def _patch_get_nearest_expiry(
    instrument_name: str,
    today: real_dt.date,
    instruments: list[dict],
    is_trading_day_fn=None,
):
    """
    Run get_nearest_expiry() with fully mocked external dependencies.
    Patches use canonical module paths so results are stable regardless of
    test execution order. Returns (expiry, rolled).
    """
    if is_trading_day_fn is None:
        def is_trading_day_fn(d: real_dt.date) -> bool:
            return d.weekday() < 5

    from src.kite_client import get_nearest_expiry

    with (
        patch("src.state.redis_get", return_value=None),
        patch("src.state.redis_set"),
        patch("src.kite_client.datetime") as mock_dt,
        patch("src.kite_client.get_kite", return_value=MagicMock()),
        patch("src.kite_client._instruments_for", return_value=instruments),
        patch("src.calendar_nse.is_trading_day", side_effect=is_trading_day_fn),
    ):
        mock_dt.now.return_value = _fake_now(today)
        return get_nearest_expiry(instrument_name)


# ── Weekly instrument tests ───────────────────────────────────────────────────

def test_weekly_rollover_on_expiry_day():
    """Weekly: nearest expiry == today → roll to the next distinct expiry."""
    today    = real_dt.date(2026, 7, 7)   # Tuesday (NIFTY expiry)
    next_exp = real_dt.date(2026, 7, 14)  # following Tuesday
    instruments = _make_instruments("NIFTY", [today, next_exp])

    exp, rolled = _patch_get_nearest_expiry("NIFTY", today, instruments)

    assert exp == next_exp, f"Expected {next_exp}, got {exp}"
    assert rolled is True


def test_weekly_no_rollover_when_not_expiry_day():
    """Weekly: nearest expiry != today → return it as-is, rolled=False."""
    today       = real_dt.date(2026, 7, 6)   # Monday
    this_expiry = real_dt.date(2026, 7, 7)   # Tuesday (tomorrow)
    next_expiry = real_dt.date(2026, 7, 14)
    instruments = _make_instruments("NIFTY", [this_expiry, next_expiry])

    exp, rolled = _patch_get_nearest_expiry("NIFTY", today, instruments)

    assert exp == this_expiry
    assert rolled is False


# ── Monthly instrument tests ──────────────────────────────────────────────────

def test_monthly_rollover_when_nearest_is_today():
    """Monthly: nearest expiry == today → roll to the next monthly."""
    today     = real_dt.date(2026, 6, 25)  # Thursday — pretend it's monthly expiry
    near_exp  = real_dt.date(2026, 6, 25)
    far_exp   = real_dt.date(2026, 7, 30)
    instruments = _make_instruments("BANKNIFTY", [near_exp, far_exp])

    exp, rolled = _patch_get_nearest_expiry("BANKNIFTY", today, instruments)

    assert exp == far_exp
    assert rolled is True


def test_monthly_rollover_when_nearest_is_next_trading_day():
    """Monthly: nearest expiry == next trading day → roll."""
    today    = real_dt.date(2026, 6, 26)  # Friday
    near_exp = real_dt.date(2026, 6, 29)  # Monday = next trading day (Sat+Sun skipped)
    far_exp  = real_dt.date(2026, 7, 30)
    instruments = _make_instruments("BANKNIFTY", [near_exp, far_exp])

    def is_td(d: real_dt.date) -> bool:
        return d.weekday() < 5

    exp, rolled = _patch_get_nearest_expiry("BANKNIFTY", today, instruments,
                                             is_trading_day_fn=is_td)

    assert exp == far_exp
    assert rolled is True


def test_monthly_rollover_next_trading_day_skips_holiday():
    """Monthly: next trading day computed correctly when Friday is a holiday."""
    # Today = Thursday May 28; Fri May 29 = holiday; Sat 30 + Sun 31 = weekend;
    # Mon Jun 1 = next trading day.  Nearest expiry == Jun 1 → should roll.
    today     = real_dt.date(2026, 5, 28)  # Thursday
    near_exp  = real_dt.date(2026, 6, 1)   # Monday = next trading day
    far_exp   = real_dt.date(2026, 6, 26)
    instruments = _make_instruments("BANKNIFTY", [near_exp, far_exp])

    holiday = real_dt.date(2026, 5, 29)

    def is_td(d: real_dt.date) -> bool:
        if d.weekday() >= 5:
            return False
        return d != holiday

    exp, rolled = _patch_get_nearest_expiry("BANKNIFTY", today, instruments,
                                             is_trading_day_fn=is_td)

    assert exp == far_exp
    assert rolled is True


def test_monthly_no_rollover_when_expiry_two_or_more_days_out():
    """Monthly: nearest expiry is 2+ days away → return it, rolled=False."""
    today    = real_dt.date(2026, 6, 23)  # Tuesday
    near_exp = real_dt.date(2026, 6, 26)  # Thursday, 3 days away
    far_exp  = real_dt.date(2026, 7, 30)
    instruments = _make_instruments("BANKNIFTY", [near_exp, far_exp])

    exp, rolled = _patch_get_nearest_expiry("BANKNIFTY", today, instruments)

    assert exp == near_exp
    assert rolled is False


def test_fallback_to_candidate_when_only_one_expiry(capfd):
    """Only one distinct expiry when rollover should apply → use it, warn, don't raise."""
    today = real_dt.date(2026, 7, 7)   # Tuesday = NIFTY expiry
    instruments = _make_instruments("NIFTY", [today])  # no second expiry in dump

    exp, rolled = _patch_get_nearest_expiry("NIFTY", today, instruments)

    assert exp == today
    assert rolled is False
    captured = capfd.readouterr()
    assert "WARNING" in captured.out
