"""
Unit tests for dynamic_stock_universe.py — candidate selection, the
staleness guard on get_active_dynamic_stocks(), and strike-step resolution.
No real Kite API calls — all instrument dumps are hand-built fixtures.

NOTE: dynamic_stock_universe imports src.notifier (and, transitively, the
real `requests` package) at module scope. Other test files (test_notifier_
format.py, test_stock_payload_shape.py) rely on being the first to import
src.notifier so their sys.modules["requests"] stub wins — importing
dynamic_stock_universe at *this* module's top level would run during
collection and, being alphabetically earlier, would poison that ordering.
Kept as a lazy import inside each test function instead.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch


def _pick_candidate(*args, **kwargs):
    from src.dynamic_stock_universe import _pick_candidate as fn
    return fn(*args, **kwargs)


def _resolve_strike_step_and_lot_size(*args, **kwargs):
    from src.dynamic_stock_universe import _resolve_strike_step_and_lot_size as fn
    return fn(*args, **kwargs)


def get_active_dynamic_stocks(*args, **kwargs):
    from src.dynamic_stock_universe import get_active_dynamic_stocks as fn
    return fn(*args, **kwargs)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _nfo_rows(name: str, expiry: date, strikes: list[float], lot_size: int = 500) -> list[dict]:
    rows = []
    for strike in strikes:
        for itype in ("CE", "PE"):
            rows.append({
                "name": name,
                "expiry": expiry,
                "instrument_type": itype,
                "strike": strike,
                "lot_size": lot_size,
            })
    return rows


def _expiry_map(names: list[str], expiry: date) -> dict[str, list[date]]:
    return {name: [expiry] for name in names}


# ── _pick_candidate ────────────────────────────────────────────────────────

class TestPickCandidate:
    def test_skips_excluded_name_and_continues_to_next(self):
        expiry = date.today() + timedelta(days=20)
        ranked = [("ALPHA", 5.0, 100.0), ("BETA", 4.0, 200.0)]
        expiry_map = _expiry_map(["ALPHA", "BETA"], expiry)
        equity_tokens = {"ALPHA": 111, "BETA": 222}
        instruments_nfo = (
            _nfo_rows("ALPHA", expiry, [95, 100, 105])
            + _nfo_rows("BETA", expiry, [195, 200, 205])
        )

        pick = _pick_candidate(
            ranked, exclude_names={"ALPHA"}, expiry_map=expiry_map,
            equity_tokens=equity_tokens, instruments_nfo=instruments_nfo,
            max_tries=5,
        )

        assert pick is not None
        assert pick["name"] == "BETA"

    def test_returns_none_after_exhausting_max_tries(self):
        # 6 candidates, none resolvable (no strikes listed for any of them),
        # max_tries=5 must give up after the 5th and never try the 6th.
        expiry = date.today() + timedelta(days=20)
        names = [f"STK{i}" for i in range(6)]
        ranked = [(name, 1.0, 100.0) for name in names]
        expiry_map = _expiry_map(names, expiry)
        equity_tokens = {name: idx for idx, name in enumerate(names)}
        instruments_nfo: list[dict] = []  # no strikes for anyone -> always unresolvable

        pick = _pick_candidate(
            ranked, exclude_names=set(), expiry_map=expiry_map,
            equity_tokens=equity_tokens, instruments_nfo=instruments_nfo,
            max_tries=5,
        )

        assert pick is None

    def test_max_tries_only_counts_non_excluded_candidates(self):
        # exclude_names entries must not consume a "try" — only real
        # resolution attempts count toward max_tries.
        expiry = date.today() + timedelta(days=20)
        ranked = [("EXCLUDED", 9.0, 100.0), ("GOOD", 8.0, 100.0)]
        expiry_map = _expiry_map(["EXCLUDED", "GOOD"], expiry)
        equity_tokens = {"EXCLUDED": 1, "GOOD": 2}
        instruments_nfo = _nfo_rows("GOOD", expiry, [95, 100, 105])

        pick = _pick_candidate(
            ranked, exclude_names={"EXCLUDED"}, expiry_map=expiry_map,
            equity_tokens=equity_tokens, instruments_nfo=instruments_nfo,
            max_tries=1,
        )

        assert pick is not None
        assert pick["name"] == "GOOD"


# ── get_active_dynamic_stocks ─────────────────────────────────────────────

class TestGetActiveDynamicStocks:
    def test_returns_empty_when_key_missing(self):
        with patch("src.dynamic_stock_universe.state.redis_get", return_value=None):
            assert get_active_dynamic_stocks() == []

    def test_returns_empty_when_date_is_stale(self):
        # This is the specific bug this test must catch: a failed EOD job
        # must never let yesterday's picks leak into today's run.
        stale_payload = {
            "date": (date.today() - timedelta(days=1)).isoformat(),
            "picks": [{"name": "STALE", "is_dynamic": True}],
        }
        with patch("src.dynamic_stock_universe.state.redis_get",
                   return_value=json.dumps(stale_payload)):
            assert get_active_dynamic_stocks() == []

    def test_returns_picks_when_date_matches_today(self):
        fresh_payload = {
            "date": date.today().isoformat(),
            "picks": [{"name": "FRESH", "is_dynamic": True}],
        }
        with patch("src.dynamic_stock_universe.state.redis_get",
                   return_value=json.dumps(fresh_payload)):
            picks = get_active_dynamic_stocks()
        assert picks == [{"name": "FRESH", "is_dynamic": True}]

    def test_returns_empty_on_malformed_json(self):
        with patch("src.dynamic_stock_universe.state.redis_get",
                   return_value="not valid json"):
            assert get_active_dynamic_stocks() == []


# ── _resolve_strike_step_and_lot_size ──────────────────────────────────────

class TestResolveStrikeStepAndLotSize:
    def test_returns_none_for_fewer_than_two_strikes(self):
        expiry = date.today() + timedelta(days=20)
        instruments_nfo = _nfo_rows("ALPHA", expiry, [100])
        assert _resolve_strike_step_and_lot_size("ALPHA", expiry, instruments_nfo) is None

    def test_returns_none_for_zero_strikes(self):
        expiry = date.today() + timedelta(days=20)
        assert _resolve_strike_step_and_lot_size("ALPHA", expiry, []) is None

    def test_resolves_step_and_lot_size_for_two_plus_strikes(self):
        expiry = date.today() + timedelta(days=20)
        instruments_nfo = _nfo_rows("ALPHA", expiry, [90, 95, 100, 105], lot_size=750)
        result = _resolve_strike_step_and_lot_size("ALPHA", expiry, instruments_nfo)
        assert result == (5, 750)


# ── Gainer/loser mutual exclusion ──────────────────────────────────────────

class TestGainerLoserMutualExclusion:
    """Sanity check on the exclude_for_loser set construction in
    compute_and_cache_dynamic_universe(): the top gainer and top loser can
    never resolve to the same stock."""

    def test_loser_pick_excludes_the_already_chosen_gainer(self):
        expiry = date.today() + timedelta(days=20)
        # SOLO is both the #1 gainer AND, if not excluded, would also be
        # the #1 loser candidate reachable from the ranked-losers list.
        ranked_gainers = [("SOLO", 10.0, 100.0), ("OTHER", 5.0, 100.0)]
        ranked_losers = [("SOLO", 10.0, 100.0), ("OTHER", 5.0, 100.0)]
        expiry_map = _expiry_map(["SOLO", "OTHER"], expiry)
        equity_tokens = {"SOLO": 1, "OTHER": 2}
        instruments_nfo = (
            _nfo_rows("SOLO", expiry, [95, 100, 105])
            + _nfo_rows("OTHER", expiry, [95, 100, 105])
        )
        static_names: set[str] = set()

        gainer = _pick_candidate(
            ranked_gainers, static_names, expiry_map, equity_tokens,
            instruments_nfo, max_tries=5,
        )
        exclude_for_loser = set(static_names)
        if gainer:
            exclude_for_loser.add(gainer["name"])
        loser = _pick_candidate(
            ranked_losers, exclude_for_loser, expiry_map, equity_tokens,
            instruments_nfo, max_tries=5,
        )

        assert gainer["name"] == "SOLO"
        assert loser["name"] == "OTHER"
        assert gainer["name"] != loser["name"]
