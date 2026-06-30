"""Unit tests for calendar_nse.is_expiry_day() and in_eval_window_for()."""
import sys
import types
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

# Stub kiteconnect before importing anything from src
if "kiteconnect" not in sys.modules:
    _kc = types.ModuleType("kiteconnect")
    _kc.KiteConnect = type("KiteConnect", (), {"__init__": lambda s, **kw: None})
    sys.modules["kiteconnect"] = _kc

IST = ZoneInfo("Asia/Kolkata")


def _dt(hour: int, minute: int, d: date | None = None) -> datetime:
    """Build a tz-aware IST datetime for the given time on `d` (defaults to today)."""
    if d is None:
        d = date(2026, 7, 7)  # arbitrary non-holiday Tuesday
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


# ── is_expiry_day ─────────────────────────────────────────────────────────────

class TestIsExpiryDayNifty(unittest.TestCase):
    """NIFTY: weekly Tuesday expiry."""

    def test_tuesday_is_expiry(self):
        # July 7 2026 is a Tuesday, assumed non-holiday
        with patch("src.calendar_nse.is_trading_day", return_value=True):
            from src.calendar_nse import is_expiry_day
            self.assertTrue(is_expiry_day("NIFTY", date(2026, 7, 7)))

    def test_wednesday_is_not_expiry(self):
        with patch("src.calendar_nse.is_trading_day", return_value=True):
            from src.calendar_nse import is_expiry_day
            self.assertFalse(is_expiry_day("NIFTY", date(2026, 7, 8)))

    def test_monday_is_not_expiry(self):
        with patch("src.calendar_nse.is_trading_day", return_value=True):
            from src.calendar_nse import is_expiry_day
            self.assertFalse(is_expiry_day("NIFTY", date(2026, 7, 6)))


class TestIsExpiryDaySensex(unittest.TestCase):
    """SENSEX: weekly Thursday expiry."""

    def test_thursday_is_expiry(self):
        # July 2 2026 is a Thursday
        with patch("src.calendar_nse.is_trading_day", return_value=True):
            from src.calendar_nse import is_expiry_day
            self.assertTrue(is_expiry_day("SENSEX", date(2026, 7, 2)))

    def test_tuesday_is_not_expiry(self):
        with patch("src.calendar_nse.is_trading_day", return_value=True):
            from src.calendar_nse import is_expiry_day
            self.assertFalse(is_expiry_day("SENSEX", date(2026, 7, 7)))

    def test_friday_is_not_expiry(self):
        with patch("src.calendar_nse.is_trading_day", return_value=True):
            from src.calendar_nse import is_expiry_day
            self.assertFalse(is_expiry_day("SENSEX", date(2026, 7, 3)))


class TestIsExpiryDayMonthly(unittest.TestCase):
    """BANKNIFTY and stocks: monthly last-trading-Thursday expiry."""

    def test_banknifty_last_thu_of_month(self):
        # June 2026: last Thursday = June 25 (non-holiday)
        # June 30 is Tuesday; last Thursday is June 25
        def _trading(d=None):
            return True  # all days are trading days for this test

        with patch("src.calendar_nse.is_trading_day", side_effect=_trading):
            from src import calendar_nse
            # Reload to pick up patched is_trading_day via is_last_trading_thursday
            self.assertTrue(calendar_nse.is_expiry_day("BANKNIFTY", date(2026, 6, 25)))

    def test_banknifty_non_expiry_thursday(self):
        # June 18 is also a Thursday but NOT the last one
        def _trading(d=None):
            return True

        with patch("src.calendar_nse.is_trading_day", side_effect=_trading):
            from src import calendar_nse
            self.assertFalse(calendar_nse.is_expiry_day("BANKNIFTY", date(2026, 6, 18)))

    def test_stock_reliance_last_thu_of_month(self):
        def _trading(d=None):
            return True

        with patch("src.calendar_nse.is_trading_day", side_effect=_trading):
            from src import calendar_nse
            self.assertTrue(calendar_nse.is_expiry_day("RELIANCE", date(2026, 6, 25)))

    def test_holiday_adjusted_banknifty(self):
        """If last Thursday of month is a holiday, expiry shifts to Wednesday."""
        # July 2026: last Thursday = July 30
        # We mock July 30 as holiday (not trading day), July 29 as trading day
        def _trading(d=None):
            if d is None:
                d = date.today()
            if d == date(2026, 7, 30):
                return False  # holiday
            return d.weekday() < 5  # weekdays are trading days

        with patch("src.calendar_nse.is_trading_day", side_effect=_trading):
            from src import calendar_nse
            # July 29 (Wednesday) should now be expiry day
            self.assertTrue(calendar_nse.is_expiry_day("BANKNIFTY", date(2026, 7, 29)))
            # July 30 (Thursday-holiday) should NOT be expiry
            self.assertFalse(calendar_nse.is_expiry_day("BANKNIFTY", date(2026, 7, 30)))

    def test_unknown_instrument_returns_false(self):
        with patch("src.calendar_nse.is_trading_day", return_value=True):
            from src.calendar_nse import is_expiry_day
            self.assertFalse(is_expiry_day("UNKNOWN_INSTRUMENT", date(2026, 7, 7)))


# ── in_eval_window_for ────────────────────────────────────────────────────────

class TestInEvalWindowFor(unittest.TestCase):
    """Per-instrument eval window: expiry-day cutoff at 13:30 vs normal 14:45."""

    def test_13_00_on_expiry_day_is_in_window(self):
        """13:00 is within 09:40–13:30 on expiry day."""
        d = date(2026, 7, 7)
        now = _dt(13, 0, d)
        with patch("src.calendar_nse.is_expiry_day", return_value=True):
            from src import calendar_nse
            self.assertTrue(calendar_nse.in_eval_window_for("NIFTY", now))

    def test_13_31_on_expiry_day_is_outside_window(self):
        """13:31 is past 13:30 cutoff on expiry day."""
        d = date(2026, 7, 7)
        now = _dt(13, 31, d)
        with patch("src.calendar_nse.is_expiry_day", return_value=True):
            from src import calendar_nse
            self.assertFalse(calendar_nse.in_eval_window_for("NIFTY", now))

    def test_13_31_on_non_expiry_day_is_in_window(self):
        """13:31 is within normal 09:40–14:45 window on non-expiry day."""
        d = date(2026, 7, 7)
        now = _dt(13, 31, d)
        with patch("src.calendar_nse.is_expiry_day", return_value=False):
            from src import calendar_nse
            self.assertTrue(calendar_nse.in_eval_window_for("NIFTY", now))

    def test_14_30_on_non_expiry_day_is_in_window(self):
        """14:30 is within normal window on non-expiry day."""
        d = date(2026, 7, 7)
        now = _dt(14, 30, d)
        with patch("src.calendar_nse.is_expiry_day", return_value=False):
            from src import calendar_nse
            self.assertTrue(calendar_nse.in_eval_window_for("NIFTY", now))

    def test_14_30_on_expiry_day_is_outside_window(self):
        """14:30 is past 13:30 cutoff on expiry day."""
        d = date(2026, 7, 7)
        now = _dt(14, 30, d)
        with patch("src.calendar_nse.is_expiry_day", return_value=True):
            from src import calendar_nse
            self.assertFalse(calendar_nse.in_eval_window_for("NIFTY", now))

    def test_before_window_start_always_false(self):
        """09:00 is before 09:40 regardless of expiry."""
        d = date(2026, 7, 7)
        now = _dt(9, 0, d)
        for expiry in (True, False):
            with patch("src.calendar_nse.is_expiry_day", return_value=expiry):
                from src import calendar_nse
                self.assertFalse(calendar_nse.in_eval_window_for("NIFTY", now))


if __name__ == "__main__":
    unittest.main()
