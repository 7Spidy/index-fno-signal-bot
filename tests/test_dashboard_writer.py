"""
Unit tests: src/dashboard_writer._build_strategy_modules_block()

Covers the read-only Worst-Faller + Dynamic Universe export added to
docs/dashboard.json. Both sources are Redis JSON blobs read via
src.state.redis_get — mocked here so no real Upstash call is made.
"""
import json
import unittest.mock as mock

from src import dashboard_writer, state


def _patch_redis_get(values: dict):
    """Returns a function usable as state.redis_get's side_effect: keyed by
    Redis key, returns the configured raw string (or None if absent)."""
    def _get(key):
        return values.get(key)
    return _get


def test_both_absent_returns_none_none():
    with mock.patch.object(state, "redis_get", side_effect=_patch_redis_get({})):
        result = dashboard_writer._build_strategy_modules_block()
    assert result == {"worst_faller": None, "dynamic_universe": None}


def test_worst_faller_populated_from_valid_json():
    wf_payload = {
        "name": "SBIN",
        "entry_time": "2026-07-31T15:00:04.123456+05:30",
        "entry_spot": 812.4,
        "target_pts": 6.5,
        "target_source": "atr",
        "pe_symbol": "SBIN26AUGPE",  # not part of the exported shape
    }
    values = {"worst_faller:position": json.dumps(wf_payload)}
    with mock.patch.object(state, "redis_get", side_effect=_patch_redis_get(values)):
        result = dashboard_writer._build_strategy_modules_block()
    assert result["dynamic_universe"] is None
    assert result["worst_faller"] == {
        "name": "SBIN",
        "entry_time": "2026-07-31T15:00:04.123456+05:30",
        "entry_spot": 812.4,
        "target_pts": 6.5,
        "target_source": "atr",
    }


def test_dynamic_universe_splits_gainer_and_loser_by_direction_restriction():
    du_payload = {
        "date": "2026-08-01",
        "gainer_found": True,
        "loser_found": True,
        "picks": [
            {"name": "TATAMOTORS", "direction_restriction": "CE_ONLY"},
            {"name": "ZOMATO", "direction_restriction": "PE_ONLY"},
        ],
    }
    values = {"stock:dynamic_universe": json.dumps(du_payload)}
    with mock.patch.object(state, "redis_get", side_effect=_patch_redis_get(values)):
        result = dashboard_writer._build_strategy_modules_block()
    assert result["worst_faller"] is None
    assert result["dynamic_universe"] == {
        "valid_for_date": "2026-08-01",
        "gainer_found": True,
        "loser_found": True,
        "gainer_name": "TATAMOTORS",
        "loser_name": "ZOMATO",
    }


def test_dynamic_universe_missing_gainer_or_loser_is_none():
    du_payload = {
        "date": "2026-08-01",
        "gainer_found": False,
        "loser_found": True,
        "picks": [
            {"name": "ZOMATO", "direction_restriction": "PE_ONLY"},
        ],
    }
    values = {"stock:dynamic_universe": json.dumps(du_payload)}
    with mock.patch.object(state, "redis_get", side_effect=_patch_redis_get(values)):
        result = dashboard_writer._build_strategy_modules_block()
    assert result["dynamic_universe"]["gainer_name"] is None
    assert result["dynamic_universe"]["loser_name"] == "ZOMATO"


def test_malformed_worst_faller_json_degrades_to_none():
    values = {"worst_faller:position": "{not valid json"}
    with mock.patch.object(state, "redis_get", side_effect=_patch_redis_get(values)):
        result = dashboard_writer._build_strategy_modules_block()
    assert result["worst_faller"] is None


def test_malformed_dynamic_universe_json_degrades_to_none():
    values = {"stock:dynamic_universe": "{not valid json"}
    with mock.patch.object(state, "redis_get", side_effect=_patch_redis_get(values)):
        result = dashboard_writer._build_strategy_modules_block()
    assert result["dynamic_universe"] is None


def test_both_present_populates_both_independently():
    wf_payload = {
        "name": "HINDALCO", "entry_time": "2026-07-31T15:00:00+05:30",
        "entry_spot": 700.0, "target_pts": 5.0, "target_source": "fallback_1.5R",
    }
    du_payload = {
        "date": "2026-08-01", "gainer_found": True, "loser_found": False,
        "picks": [{"name": "INFY", "direction_restriction": "CE_ONLY"}],
    }
    values = {
        "worst_faller:position": json.dumps(wf_payload),
        "stock:dynamic_universe": json.dumps(du_payload),
    }
    with mock.patch.object(state, "redis_get", side_effect=_patch_redis_get(values)):
        result = dashboard_writer._build_strategy_modules_block()
    assert result["worst_faller"]["name"] == "HINDALCO"
    assert result["dynamic_universe"]["gainer_name"] == "INFY"
    assert result["dynamic_universe"]["loser_name"] is None
