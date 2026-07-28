"""Unit tests for position_tracker.py — ladder function, SL invariants, and
the pull-based discovery / confirm / exit flow."""
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from src import position_tracker
from src.position_tracker import compute_final_sl, compute_ladder_sl


# ──────────────────────────────────────────────────────────────
# Ladder table — exact values from spec
# ──────────────────────────────────────────────────────────────

class TestLadderExactValues:
    """The spec defines a fixed table of (progress, sl_fraction) pairs.
    Verify each breakpoint for both CE and PE directions.
    """

    # entry=100, T=100, so sl_price = 100 + sl_fraction*100 for CE
    # progress = (current_price - entry) / T for CE

    def _ce(self, progress, prior_sl=0.0):
        """CE: entry=100, T=100, current_price = 100 + progress*100."""
        entry = 100.0
        T = 100.0
        current_price = entry + progress * T
        return compute_ladder_sl(entry, T, current_price, "CE", prior_sl)

    def _pe(self, progress, prior_sl=9999.0):
        """PE: entry=100, T=100, current_price = 100 - progress*100."""
        entry = 100.0
        T = 100.0
        current_price = entry - progress * T
        return compute_ladder_sl(entry, T, current_price, "PE", prior_sl)

    def test_progress_below_0_5_returns_prior_sl_ce(self):
        assert self._ce(0.49, prior_sl=90.0) == 90.0

    def test_progress_below_0_5_returns_prior_sl_pe(self):
        assert self._pe(0.49, prior_sl=110.0) == 110.0

    def test_progress_0_5_sl_fraction_0_25_ce(self):
        # sl_price = 100 + 0.25*100 = 125
        result = self._ce(0.5)
        assert abs(result - 125.0) < 1e-9

    def test_progress_0_5_sl_fraction_0_25_pe(self):
        # sl_price = 100 - 0.25*100 = 75
        result = self._pe(0.5)
        assert abs(result - 75.0) < 1e-9

    def test_progress_0_9_sl_fraction_0_6_ce(self):
        # sl_price = 100 + 0.6*100 = 160
        result = self._ce(0.9)
        assert abs(result - 160.0) < 1e-9

    def test_progress_0_9_sl_fraction_0_6_pe(self):
        # sl_price = 100 - 0.6*100 = 40
        result = self._pe(0.9)
        assert abs(result - 40.0) < 1e-9

    def test_progress_1_0_sl_fraction_0_9_ce(self):
        # sl_price = 100 + 0.9*100 = 190
        result = self._ce(1.0)
        assert abs(result - 190.0) < 1e-9

    def test_progress_1_0_sl_fraction_0_9_pe(self):
        # sl_price = 100 - 0.9*100 = 10
        result = self._pe(1.0)
        assert abs(result - 10.0) < 1e-9

    def test_progress_1_1_sl_fraction_1_0_ce(self):
        # n=1, sl_fraction = 0.9 + 0.1*1 = 1.0 → sl_price = 100 + 100 = 200
        result = self._ce(1.1)
        assert abs(result - 200.0) < 1e-9

    def test_progress_1_1_sl_fraction_1_0_pe(self):
        # n=1, sl_fraction=1.0 → sl_price = 100 - 100 = 0
        result = self._pe(1.1)
        assert abs(result - 0.0) < 1e-9

    def test_progress_1_2_sl_fraction_1_1_ce(self):
        # n=2, sl_fraction = 0.9 + 0.1*2 = 1.1 → sl_price = 100 + 110 = 210
        result = self._ce(1.2)
        assert abs(result - 210.0) < 1e-9

    def test_progress_1_2_sl_fraction_1_1_pe(self):
        # n=2, sl_fraction=1.1 → sl_price = 100 - 110 = -10
        result = self._pe(1.2)
        assert abs(result - (-10.0)) < 1e-9

    def test_large_progress_ce(self):
        # progress=2.3 → n = floor((2.3-1.0)/0.1) = floor(13) = 13
        # sl_fraction = 0.9 + 0.1*13 = 2.2 → sl_price = 100 + 220 = 320
        result = self._ce(2.3)
        assert abs(result - 320.0) < 1e-9


# ──────────────────────────────────────────────────────────────
# Monotonicity
# ──────────────────────────────────────────────────────────────

class TestMonotonicity:
    """Calling compute_ladder_sl with a lower current_price (CE) after a higher
    one must never decrease the returned SL (monotonic non-decreasing for CE,
    non-increasing for PE).
    """

    def test_ce_monotonic_ascending(self):
        entry, T = 100.0, 100.0
        prior_sl = 0.0
        prices = [145, 155, 165, 140, 150]   # price dips then rises
        sls = []
        for p in prices:
            sl = compute_ladder_sl(entry, T, p, "CE", prior_sl)
            sls.append(sl)
            prior_sl = sl
        # Every SL must be >= the previous one
        for i in range(1, len(sls)):
            assert sls[i] >= sls[i - 1], f"SL not monotonic at step {i}: {sls}"

    def test_pe_monotonic_descending(self):
        entry, T = 100.0, 100.0
        prior_sl = 9999.0
        prices = [55, 45, 35, 50, 40]   # price bounces but trends down
        sls = []
        for p in prices:
            sl = compute_ladder_sl(entry, T, p, "PE", prior_sl)
            sls.append(sl)
            prior_sl = sl
        for i in range(1, len(sls)):
            assert sls[i] <= sls[i - 1], f"SL not monotonic at step {i}: {sls}"

    def test_ce_never_decreases_when_price_drops_below_entry(self):
        # Price fell back below entry — ladder should not be reset
        entry, T = 100.0, 100.0
        # First call at progress 0.6 → sl set to 125
        sl1 = compute_ladder_sl(entry, T, 160.0, "CE", 0.0)
        # Second call: price drops to 80 (below entry)
        sl2 = compute_ladder_sl(entry, T, 80.0, "CE", sl1)
        assert sl2 >= sl1


# ──────────────────────────────────────────────────────────────
# compute_final_sl
# ──────────────────────────────────────────────────────────────

class TestComputeFinalSl:
    def test_ce_takes_max(self):
        assert compute_final_sl(150.0, 160.0, "CE") == 160.0
        assert compute_final_sl(160.0, 150.0, "CE") == 160.0

    def test_pe_takes_min(self):
        assert compute_final_sl(50.0, 40.0, "PE") == 40.0
        assert compute_final_sl(40.0, 50.0, "PE") == 40.0

    def test_ce_final_never_worse_than_ladder(self):
        ladder = 150.0
        ai     = 145.0   # AI mistakenly looser — but compute_final_sl should correct
        result = compute_final_sl(ladder, ai, "CE")
        assert result >= ladder

    def test_pe_final_never_worse_than_ladder(self):
        ladder = 50.0
        ai     = 55.0   # AI mistakenly looser
        result = compute_final_sl(ladder, ai, "PE")
        assert result <= ladder


# ──────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            compute_ladder_sl(100.0, 100.0, 150.0, "LONG", 90.0)

    def test_t_zero_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, 0.0, 150.0, "CE", 90.0)
        assert result == 90.0

    def test_t_negative_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, -10.0, 150.0, "CE", 90.0)
        assert result == 90.0

    def test_t_none_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, None, 150.0, "CE", 90.0)
        assert result == 90.0

    def test_current_price_none_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, 100.0, None, "CE", 90.0)
        assert result == 90.0

    def test_direction_case_insensitive(self):
        r1 = compute_ladder_sl(100.0, 100.0, 160.0, "ce", 0.0)
        r2 = compute_ladder_sl(100.0, 100.0, 160.0, "CE", 0.0)
        assert r1 == r2


# ──────────────────────────────────────────────────────────────
# SL can legitimately exceed the original T value
# ──────────────────────────────────────────────────────────────

class TestSlExceedsOriginalT:
    """Confirm that SL can exceed the original T value when price runs far
    past 1.0T — this is intended behaviour with T fixed as the denominator.
    No special-casing needed: it falls out naturally from the +0.1 per +0.1T
    ladder step beyond 1.0T.
    """

    def test_sl_exceeds_original_t_ce(self):
        # entry=100, T=100 (original, fixed), progress=1.8 (price=280)
        # n = floor((1.8 - 1.0) / 0.1) = 8 → sl_fraction = 0.9 + 0.8 = 1.7
        # sl_price = 100 + 1.7 * 100 = 270 > entry+T = 200
        entry = 100.0
        T = 100.0
        price_at_1_8T = entry + 1.8 * T  # 280
        sl = compute_ladder_sl(entry, T, price_at_1_8T, "CE", 0.0)
        assert sl > entry + T, f"Expected SL {sl} > entry+T {entry + T}"
        assert abs(sl - 270.0) < 1e-9

    def test_sl_exceeds_original_t_pe(self):
        # entry=100, T=100, progress=1.8 (price=−80)
        # sl_fraction = 1.7 → sl_price = 100 - 1.7*100 = −70 < entry−T = 0
        entry = 100.0
        T = 100.0
        price_at_1_8T = entry - 1.8 * T  # −80
        sl = compute_ladder_sl(entry, T, price_at_1_8T, "PE", 9999.0)
        assert sl < entry - T, f"Expected SL {sl} < entry-T {entry - T}"
        assert abs(sl - (-70.0)) < 1e-9


# ──────────────────────────────────────────────────────────────
# Redis keying — guards against the single-position-per-underlying bug
# ──────────────────────────────────────────────────────────────

class TestPerTradingsymbolKeying:
    """State must be keyed by tradingsymbol, not by underlying name, so
    concurrent NIFTY CE and NIFTY PE positions are tracked independently."""

    def test_position_key_is_per_tradingsymbol(self):
        ce_key = position_tracker._position_key("NIFTY26JUN24600CE")
        pe_key = position_tracker._position_key("NIFTY26JUN24500PE")
        assert ce_key != pe_key
        assert ce_key == "position:NIFTY26JUN24600CE"


# ──────────────────────────────────────────────────────────────
# Underlying / asset-class extraction across index AND stock instruments
# — this is the direct regression coverage for the MARUTI incident, where
# stock positions and non-NIFTY index positions were silently invisible to
# the tracker.
# ──────────────────────────────────────────────────────────────

class TestUnderlyingExtractionMultiInstrument:
    def test_index_names_resolve(self):
        assert position_tracker._underlying_from_tradingsymbol("NIFTY26JUN24600CE") == "NIFTY"
        assert position_tracker._underlying_from_tradingsymbol("BANKNIFTY26JUN52500PE") == "BANKNIFTY"
        assert position_tracker._underlying_from_tradingsymbol("SENSEX26JUN82000CE") == "SENSEX"

    def test_stock_names_resolve(self):
        assert position_tracker._underlying_from_tradingsymbol("MARUTI26JUL14300CE") == "MARUTI"
        assert position_tracker._underlying_from_tradingsymbol("SBIN26JUL800PE") == "SBIN"
        assert position_tracker._underlying_from_tradingsymbol("LT26JUL3600CE") == "LT"

    def test_unknown_symbol_returns_none(self):
        assert position_tracker._underlying_from_tradingsymbol("RANDOMJUNK26JUL100CE") is None

    def test_midcpnifty_finnifty_no_longer_recognised(self):
        # Deliberately dropped — never in config.INSTRUMENTS, were dead entries.
        assert position_tracker._underlying_from_tradingsymbol("MIDCPNIFTY26JUL12000CE") is None
        assert position_tracker._underlying_from_tradingsymbol("FINNIFTY26JUL22000PE") is None


class TestAssetClassFor:
    def test_index_names_are_index(self):
        for name in ("NIFTY", "BANKNIFTY", "SENSEX"):
            assert position_tracker._asset_class_for(name) == "INDEX"

    def test_stock_names_are_stock(self):
        for name in ("MARUTI", "SBIN", "RELIANCE"):
            assert position_tracker._asset_class_for(name) == "STOCK"

    def test_unknown_is_unknown(self):
        assert position_tracker._asset_class_for("GARBAGE") == "UNKNOWN"


# ──────────────────────────────────────────────────────────────
# Intent lookup — legacy executor keys vs the new per-instrument tracker key
# ──────────────────────────────────────────────────────────────

class TestLoadTrackerIntent:
    def test_returns_matching_payload(self):
        payload = json.dumps({"instrument": "MARUTI", "target_pts": 45.0, "spot_sl": 12000.0})
        with patch("src.position_tracker.state.redis_get", return_value=payload):
            result = position_tracker._load_tracker_intent("MARUTI")
        assert result["target_pts"] == 45.0

    def test_mismatched_instrument_returns_none(self):
        payload = json.dumps({"instrument": "SBIN", "target_pts": 10.0})
        with patch("src.position_tracker.state.redis_get", return_value=payload):
            assert position_tracker._load_tracker_intent("MARUTI") is None

    def test_missing_key_returns_none(self):
        with patch("src.position_tracker.state.redis_get", return_value=None):
            assert position_tracker._load_tracker_intent("MARUTI") is None

    def test_malformed_json_returns_none(self):
        with patch("src.position_tracker.state.redis_get", return_value="not json"):
            assert position_tracker._load_tracker_intent("MARUTI") is None


class TestLoadIntentPriorityFallback:
    def test_legacy_executor_key_wins_over_tracker_key(self):
        def fake_get(key):
            if key == "executor:pending_intent":
                return json.dumps({"instrument": "NIFTY", "spot_risk_pts": 20.0, "target_rr": 1.5})
            if key.startswith("tracker:pending_intent:"):
                return json.dumps({"instrument": "NIFTY", "target_pts": 999.0})
            return None
        with patch("src.position_tracker.state.redis_get", side_effect=fake_get):
            result = position_tracker._load_intent("NIFTY")
        assert result["spot_risk_pts"] == 20.0  # legacy payload, not the tracker one

    def test_falls_back_to_tracker_key_when_legacy_absent(self):
        def fake_get(key):
            if key in ("executor:pending_intent", "executor:position"):
                return None
            if key == "tracker:pending_intent:BANKNIFTY":
                return json.dumps({"instrument": "BANKNIFTY", "target_pts": 75.0, "spot_sl": 51000.0})
            return None
        with patch("src.position_tracker.state.redis_get", side_effect=fake_get):
            result = position_tracker._load_intent("BANKNIFTY")
        assert result["target_pts"] == 75.0

    def test_none_when_neither_source_matches(self):
        with patch("src.position_tracker.state.redis_get", return_value=None):
            assert position_tracker._load_intent("SENSEX") is None


# ──────────────────────────────────────────────────────────────
# _get_rsi_snapshot — asset_class branch (INDEX futures vs STOCK equity)
# ──────────────────────────────────────────────────────────────

class TestGetRsiSnapshotAssetClassBranch:
    def test_index_path_reads_futures_token(self):
        import pandas as pd
        tokens_json = json.dumps({"NIFTY": {"token": 12345}})
        rsi_series = pd.Series([10.0, 20.0, 30.0])
        with patch("src.position_tracker.state.redis_get", return_value=tokens_json), \
             patch("src.kite_client.fetch_ohlcv") as mock_fetch, \
             patch("src.indicators.rsi_wilder", return_value=rsi_series):
            result = position_tracker._get_rsi_snapshot(
                "NIFTY", datetime(2026, 7, 2, 9, 15), asset_class="INDEX"
            )
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args[0][0] == 12345
        assert result == [10.0, 20.0, 30.0]

    def test_stock_path_reads_equity_token_directly(self):
        import pandas as pd
        tokens_json = json.dumps({"MARUTI": 67890})   # flat int, not nested dict
        rsi_series = pd.Series([10.0, 20.0, 30.0])
        with patch("src.position_tracker.state.redis_get", return_value=tokens_json), \
             patch("src.kite_client.fetch_ohlcv") as mock_fetch, \
             patch("src.indicators.rsi_wilder", return_value=rsi_series):
            result = position_tracker._get_rsi_snapshot(
                "MARUTI", datetime(2026, 7, 2, 9, 15), asset_class="STOCK"
            )
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args[0][0] == 67890
        assert result == [10.0, 20.0, 30.0]

    def test_stock_missing_token_returns_none(self):
        with patch("src.position_tracker.state.redis_get", return_value=json.dumps({})):
            result = position_tracker._get_rsi_snapshot(
                "MARUTI", datetime(2026, 7, 2, 9, 15), asset_class="STOCK"
            )
        assert result is None

    def test_index_missing_token_map_returns_none(self):
        with patch("src.position_tracker.state.redis_get", return_value=None):
            result = position_tracker._get_rsi_snapshot(
                "NIFTY", datetime(2026, 7, 2, 9, 15), asset_class="INDEX"
            )
        assert result is None


class TestDirectionFromTradingsymbolStockNameCollision:
    """RELIANCE and BAJFINANCE end in the letters CE as plain English words
    ('relianCE', 'bajfinanCE') — must not be misread as a call option."""

    def test_reliance_bare_stock_name_returns_none(self):
        assert position_tracker._direction_from_tradingsymbol("RELIANCE") is None

    def test_bajfinance_bare_stock_name_returns_none(self):
        assert position_tracker._direction_from_tradingsymbol("BAJFINANCE") is None

    def test_genuine_reliance_option_returns_ce(self):
        assert position_tracker._direction_from_tradingsymbol("RELIANCE26JUL1300CE") == "CE"
