"""Unit tests for src/pvwap_signals.py — pure functions only (swing
detection, zone validation, Fibonacci, bias determination, entry check,
SL calc). No Kite/Redis/Discord I/O here — see
test_pvwap_premarket_integration.py for the orchestration path."""
import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src import pvwap_signals

IST = ZoneInfo("Asia/Kolkata")


def _hourly_df(highs, lows, closes=None):
    n = len(highs)
    closes = closes or [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="1h"),
        "open":   closes,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": np.ones(n) * 1000,
    })


class TestDetectSwings:
    def test_finds_swing_high_and_low(self):
        highs = [10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10]
        lows  = [5,  6,  7,  8,  7,  6,  5,  1, 3, 4, 5]
        df = _hourly_df(highs, lows)
        swings = pvwap_signals.detect_swings(df, window=2)

        highs_found = [s for s in swings if s["type"] == "high"]
        lows_found  = [s for s in swings if s["type"] == "low"]
        assert any(s["index"] == 3 and s["price"] == 13 for s in highs_found)
        assert any(s["index"] == 7 and s["price"] == 1 for s in lows_found)

    def test_flat_series_has_no_swings(self):
        df = _hourly_df([10] * 15, [5] * 15)
        swings = pvwap_signals.detect_swings(df, window=3)
        assert swings == []

    def test_short_df_returns_empty(self):
        df = _hourly_df([10, 11, 12], [5, 6, 7])
        swings = pvwap_signals.detect_swings(df, window=5)
        assert swings == []


class TestValidateZones:
    def test_clusters_touches_within_tolerance_into_a_zone(self):
        swings = [
            {"index": 0, "price": 100.0, "type": "high", "timestamp": None},
            {"index": 5, "price": 100.1, "type": "high", "timestamp": None},
            {"index": 9, "price": 99.9,  "type": "high", "timestamp": None},
        ]
        zones = pvwap_signals.validate_zones(swings, tolerance_pct=0.5, min_touches=2)
        assert len(zones) == 1
        assert zones[0]["type"] == "resistance"
        assert zones[0]["touches"] == 3

    def test_single_touch_below_min_touches_is_dropped(self):
        swings = [{"index": 0, "price": 150.0, "type": "high", "timestamp": None}]
        zones = pvwap_signals.validate_zones(swings, tolerance_pct=0.15, min_touches=2)
        assert zones == []

    def test_empty_swings_returns_empty_zones(self):
        assert pvwap_signals.validate_zones([], tolerance_pct=0.15, min_touches=2) == []

    def test_lows_form_support_zone(self):
        swings = [
            {"index": 0, "price": 50.0, "type": "low", "timestamp": None},
            {"index": 3, "price": 50.05, "type": "low", "timestamp": None},
        ]
        zones = pvwap_signals.validate_zones(swings, tolerance_pct=0.5, min_touches=2)
        assert len(zones) == 1
        assert zones[0]["type"] == "support"


class TestFibonacciLevels:
    def test_standard_retracement_levels(self):
        levels = pvwap_signals.fibonacci_levels(swing_high=100.0, swing_low=0.0)
        assert levels["0.382"] == 61.8
        assert levels["0.500"] == 50.0
        assert levels["0.618"] == 38.2


class TestDetermineBias:
    def test_no_zones_defaults_to_neutral(self):
        result = pvwap_signals.determine_bias(
            previous_close=100.0, previous_support=None, premarket_open=99.0,
            zones=[], fib_levels={},
        )
        assert result["bias"] == "NEUTRAL"
        assert result["rationale"] == "no_valid_zones"

    def test_trap_gapped_down_holding_support_is_bullish(self):
        zones = [{"level": 98.0, "type": "support", "touches": 2}]
        result = pvwap_signals.determine_bias(
            previous_close=100.0, previous_support=98.0, premarket_open=98.05,
            zones=zones, fib_levels={},
        )
        assert result["bias"] == "CE"
        assert result["rationale"] == "trap"

    def test_rejection_gapped_up_pinned_at_resistance_is_bearish(self):
        zones = [{"level": 105.0, "type": "resistance", "touches": 2}]
        result = pvwap_signals.determine_bias(
            previous_close=100.0, previous_support=None, premarket_open=104.9,
            zones=zones, fib_levels={},
        )
        assert result["bias"] == "PE"
        assert result["rationale"] == "rejection"

    def test_open_far_from_any_zone_is_neutral(self):
        zones = [
            {"level": 90.0, "type": "support", "touches": 2},
            {"level": 110.0, "type": "resistance", "touches": 2},
        ]
        result = pvwap_signals.determine_bias(
            previous_close=100.0, previous_support=90.0, premarket_open=100.0,
            zones=zones, fib_levels={},
        )
        assert result["bias"] == "NEUTRAL"
        assert result["rationale"] == "neutral"


class TestCheckEntry:
    def test_neutral_bias_never_enters(self):
        assert pvwap_signals.check_entry("NEUTRAL", 100, 99, 55, 50, False) is False

    def test_position_already_open_blocks_entry(self):
        assert pvwap_signals.check_entry("CE", 100, 99, 55, 50, True) is False

    def test_ce_enters_on_price_above_vwap_and_rsi_rising(self):
        assert pvwap_signals.check_entry("CE", 100, 99, 55, 50, False) is True

    def test_ce_blocked_when_price_below_vwap(self):
        assert pvwap_signals.check_entry("CE", 98, 99, 55, 50, False) is False

    def test_ce_blocked_when_rsi_falling(self):
        assert pvwap_signals.check_entry("CE", 100, 99, 50, 55, False) is False

    def test_pe_enters_on_price_below_vwap_and_rsi_falling(self):
        assert pvwap_signals.check_entry("PE", 98, 99, 45, 50, False) is True

    def test_pe_blocked_when_price_above_vwap(self):
        assert pvwap_signals.check_entry("PE", 100, 99, 45, 50, False) is False

    def test_pe_blocked_when_rsi_rising(self):
        assert pvwap_signals.check_entry("PE", 98, 99, 55, 50, False) is False


class TestComputeSl:
    def _five_min_df(self):
        return pd.DataFrame({
            "timestamp": pd.date_range("2024-01-01 09:15", periods=8, freq="5min"),
            "open":  [100, 101, 99, 102, 98, 103, 97, 104],
            "high":  [101, 102, 100, 103, 99, 104, 98, 105],
            "low":   [99, 100, 98, 101, 90, 102, 96, 103],
            "close": [100.5, 101.5, 99.5, 102.5, 98.5, 103.5, 97.5, 104.5],
            "volume": [1000] * 8,
        })

    def test_ce_sl_is_min_low_of_last_n_candles(self):
        df = self._five_min_df()
        sl = pvwap_signals.compute_sl(df, "CE", candles=5)
        # last 5 candles' lows: [101, 90, 102, 96, 103] -> min 90
        assert sl == 90.0

    def test_pe_sl_is_max_high_of_last_n_candles(self):
        df = self._five_min_df()
        sl = pvwap_signals.compute_sl(df, "PE", candles=5)
        # last 5 candles' highs: [103, 99, 104, 98, 105] -> max 105
        assert sl == 105.0


class TestPositionOpenFlag:
    """These cover the tradingsymbol-scoped open-position flag needed once
    NIFTY can run both C1-C4 and PVWAP concurrently — an open NIFTY paper
    position no longer implies it belongs to PVWAP, so the flag must track
    PVWAP's own tradingsymbol and self-heal against paper_engine state."""

    def test_no_flag_set_means_not_open(self):
        with patch("src.pvwap_signals.state.redis_get", return_value=None):
            assert pvwap_signals.is_position_open("2026-07-16") is False

    def test_mark_open_position_writes_tradingsymbol(self):
        with patch("src.pvwap_signals.state.redis_set", return_value=True) as mock_set:
            pvwap_signals.mark_open_position(
                "2026-07-16", "NIFTY26JUL24600CE", datetime(2026, 7, 16, 9, 20, tzinfo=IST),
            )
        key, value = mock_set.call_args[0]
        assert key == "pvwap:open_position:2026-07-16"
        assert json.loads(value)["tradingsymbol"] == "NIFTY26JUL24600CE"

    def test_open_when_paper_position_still_exists(self):
        flag = json.dumps({"tradingsymbol": "NIFTY26JUL24600CE"})
        with patch("src.pvwap_signals.state.redis_get", return_value=flag), \
             patch("src.paper_engine.load_paper_position", return_value={"tradingsymbol": "NIFTY26JUL24600CE"}):
            assert pvwap_signals.is_position_open("2026-07-16") is True

    def test_self_heals_and_clears_flag_once_paper_position_closed(self):
        flag = json.dumps({"tradingsymbol": "NIFTY26JUL24600CE"})
        with patch("src.pvwap_signals.state.redis_get", return_value=flag), \
             patch("src.pvwap_signals.state.redis_delete", return_value=True) as mock_del, \
             patch("src.paper_engine.load_paper_position", return_value=None):
            assert pvwap_signals.is_position_open("2026-07-16") is False
        mock_del.assert_called_once_with("pvwap:open_position:2026-07-16")

    def test_generic_c1_c4_niftly_position_does_not_count_as_pvwap_open(self):
        # No PVWAP flag set at all, even though a C1-C4-opened NIFTY paper
        # position might exist — is_position_open must not conflate the two.
        with patch("src.pvwap_signals.state.redis_get", return_value=None), \
             patch("src.paper_engine.load_paper_position", return_value={"instrument": "NIFTY"}) as mock_load:
            assert pvwap_signals.is_position_open("2026-07-16") is False
        mock_load.assert_not_called()
