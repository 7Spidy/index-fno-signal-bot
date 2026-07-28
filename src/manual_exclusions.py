"""Manual stock exclusions — date-keyed override for the static 14-stock
and dynamic gainer/loser/worst-faller universes. Edited directly via
GitHub's file editor (deep-linked from the dashboard); read-only from here.

Fails open: any read/parse failure returns an empty exclusion set rather
than raising, so a malformed file can never block a trading run.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_EXCLUSIONS_FILE = Path(__file__).parent.parent / "config" / "manual_exclusions.json"
_exclusions: dict[str, list[str]] | None = None


def _load() -> dict[str, list[str]]:
    global _exclusions
    if _exclusions is not None:
        return _exclusions
    try:
        _exclusions = json.loads(_EXCLUSIONS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[manual_exclusions] Could not load {_EXCLUSIONS_FILE}: {e}")
        _exclusions = {}
    return _exclusions


def get_excluded_symbols_for_date(d: date | None = None) -> set[str]:
    """Symbols manually excluded for the given date (default: today, IST
    calendar date). Callers pass calendar_nse.next_trading_day() explicitly
    when filtering a pick being made *for* a future date."""
    if d is None:
        d = date.today()
    return set(_load().get(d.isoformat(), []))
