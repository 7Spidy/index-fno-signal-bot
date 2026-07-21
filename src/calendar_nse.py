"""NSE trading calendar — holiday check and eval window gate."""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src import config

IST = ZoneInfo("Asia/Kolkata")

_HOLIDAYS_FILE = Path(__file__).parent.parent / "holidays_2026.json"
_holidays: set[date] | None = None


def _load_holidays() -> set[date]:
    global _holidays
    if _holidays is not None:
        return _holidays
    try:
        raw = json.loads(_HOLIDAYS_FILE.read_text(encoding="utf-8"))
        year_key = str(date.today().year)
        entries = raw.get(year_key, raw.get("2026", []))
        _holidays = {date.fromisoformat(e["date"]) for e in entries}
    except Exception as e:
        print(f"[calendar] Could not load holidays: {e}")
        _holidays = set()
    return _holidays


def is_trading_day(d: date | None = None) -> bool:
    if d is None:
        d = datetime.now(IST).date()
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in _load_holidays()


def next_trading_day(d: date | None = None) -> date:
    """First trading day strictly after `d` (default: today, IST). Walks
    forward day by day, skipping weekends and NSE holidays via
    is_trading_day(). Used by dynamic_stock_universe.py's EOD job to tag
    cached picks with the trading day they're valid FOR, not the day the
    job happened to run on."""
    if d is None:
        d = datetime.now(IST).date()
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def in_eval_window(now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now(IST)
    start_str, end_str = config.EVAL_WINDOW_IST
    h0, m0 = map(int, start_str.split(":"))
    h1, m1 = map(int, end_str.split(":"))
    window_start = time(h0, m0)
    window_end = time(h1, m1)
    current_time = now.astimezone(IST).time()
    return window_start <= current_time <= window_end


def is_last_trading_thursday(d: date | None = None) -> bool:
    """True if d is the last Thursday of its month that is also a trading
    day (weekend/holiday-adjusted backward to the prior trading day, per
    standard NSE monthly-expiry convention)."""
    if d is None:
        d = datetime.now(IST).date()
    # Find the last Thursday of d's month
    import calendar as _cal
    last_day = _cal.monthrange(d.year, d.month)[1]
    last_thu = date(d.year, d.month, last_day)
    while last_thu.weekday() != 3:  # 3 = Thursday
        last_thu -= __import__("datetime").timedelta(days=1)
    # Adjust backward past holidays/weekends to the prior trading day
    candidate = last_thu
    while not is_trading_day(candidate):
        candidate -= __import__("datetime").timedelta(days=1)
    return d == candidate


def is_expiry_day(instrument_name: str, d: date | None = None) -> bool:
    """True if instrument_name's contract expires on date d (today by default).
    Weekly instruments (NIFTY, SENSEX): d.weekday() == WEEKLY_EXPIRY_WEEKDAY.
    Monthly instruments (BANKNIFTY, all 14 stocks): is_last_trading_thursday(d).
    Unknown instrument name → False (fail open)."""
    if d is None:
        d = datetime.now(IST).date()
    if not is_trading_day(d):
        return False

    # Weekly expiry check
    if instrument_name in config.WEEKLY_EXPIRY_WEEKDAY:
        return d.weekday() == config.WEEKLY_EXPIRY_WEEKDAY[instrument_name]

    # Monthly expiry: BANKNIFTY + all 14 stocks
    from src import stock_config
    monthly_names = config.MONTHLY_EXPIRY_INSTRUMENTS | {s["name"] for s in stock_config.STOCKS}
    if instrument_name in monthly_names:
        return is_last_trading_thursday(d)

    return False  # unknown instrument — never cut off


def in_eval_window_for(instrument_name: str, now: datetime | None = None) -> bool:
    """Per-instrument eval-window gate. Same as in_eval_window(), but if
    is_expiry_day(instrument_name, now.date()) is True, the window's effective
    end becomes config.EXPIRY_DAY_CUTOFF (13:30) instead of the normal
    EVAL_WINDOW_END. Other instruments not expiring today are unaffected."""
    if now is None:
        now = datetime.now(IST)
    now_ist = now.astimezone(IST)

    start_str, _ = config.EVAL_WINDOW_IST
    h0, m0 = map(int, start_str.split(":"))
    window_start = time(h0, m0)

    if is_expiry_day(instrument_name, now_ist.date()):
        h1, m1 = map(int, config.EXPIRY_DAY_CUTOFF.split(":"))
    else:
        end_str = config.EVAL_WINDOW_IST[1]
        h1, m1 = map(int, end_str.split(":"))
    window_end = time(h1, m1)

    current_time = now_ist.time()
    return window_start <= current_time <= window_end


if __name__ == "__main__":
    d = datetime.now(IST)
    print(f"Today ({d.date()}): trading_day={is_trading_day()}, in_window={in_eval_window()}")
