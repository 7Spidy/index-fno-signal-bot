"""NSE trading calendar — holiday check and eval window gate."""
import json
from datetime import date, datetime, time
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


if __name__ == "__main__":
    d = datetime.now(IST)
    print(f"Today ({d.date()}): trading_day={is_trading_day()}, in_window={in_eval_window()}")
