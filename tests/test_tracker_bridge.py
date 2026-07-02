"""Unit tests for tracker_bridge.py — the per-instrument Redis intent writer
consumed by position_tracker.py (decoupled from executor_bridge.py)."""
import json
from unittest.mock import patch

from src import tracker_bridge


class TestWriteTrackerIntent:
    def test_writes_payload_with_correct_key_and_ttl(self):
        with patch("src.tracker_bridge.state.redis_set", return_value=True) as mock_set:
            ok = tracker_bridge.write_tracker_intent(
                instrument="MARUTI", asset_class="STOCK", direction="CE",
                tradingsymbol="MARUTI26JUL14300CE", spot_sl=14150.0, target_pts=42.5,
            )
        assert ok is True
        key, value = mock_set.call_args[0]
        assert key == "tracker:pending_intent:MARUTI"
        assert mock_set.call_args.kwargs["ex"] == tracker_bridge.INTENT_TTL_SECONDS
        payload = json.loads(value)
        assert payload["target_pts"] == 42.5
        assert payload["asset_class"] == "STOCK"
        assert payload["instrument"] == "MARUTI"
        assert payload["direction"] == "CE"
        assert payload["spot_sl"] == 14150.0

    def test_index_signal_writes_correct_key(self):
        with patch("src.tracker_bridge.state.redis_set", return_value=True) as mock_set:
            tracker_bridge.write_tracker_intent(
                instrument="BANKNIFTY", asset_class="INDEX", direction="PE",
                tradingsymbol="BANKNIFTY26JUL52500PE", spot_sl=52700.0, target_pts=120.0,
                spot_risk_pts=80.0, target_rr=1.5, target_source="rr", atm_strike=52500,
            )
        key, value = mock_set.call_args[0]
        assert key == "tracker:pending_intent:BANKNIFTY"
        payload = json.loads(value)
        assert payload["target_rr"] == 1.5
        assert payload["target_source"] == "rr"
        assert payload["atm_strike"] == 52500

    def test_missing_tradingsymbol_skips_write(self):
        with patch("src.tracker_bridge.state.redis_set") as mock_set:
            ok = tracker_bridge.write_tracker_intent(
                instrument="MARUTI", asset_class="STOCK", direction="CE",
                tradingsymbol=None, spot_sl=14150.0, target_pts=42.5,
            )
        assert ok is False
        mock_set.assert_not_called()

    def test_missing_target_pts_skips_write(self):
        with patch("src.tracker_bridge.state.redis_set") as mock_set:
            ok = tracker_bridge.write_tracker_intent(
                instrument="NIFTY", asset_class="INDEX", direction="CE",
                tradingsymbol="NIFTY26JUL24600CE", spot_sl=24480.0, target_pts=None,
            )
        assert ok is False
        mock_set.assert_not_called()

    def test_zero_or_negative_target_pts_skips_write(self):
        with patch("src.tracker_bridge.state.redis_set") as mock_set:
            ok = tracker_bridge.write_tracker_intent(
                instrument="NIFTY", asset_class="INDEX", direction="CE",
                tradingsymbol="NIFTY26JUL24600CE", spot_sl=24480.0, target_pts=0,
            )
        assert ok is False
        mock_set.assert_not_called()

    def test_redis_failure_returns_false(self):
        with patch("src.tracker_bridge.state.redis_set", return_value=False):
            ok = tracker_bridge.write_tracker_intent(
                instrument="MARUTI", asset_class="STOCK", direction="CE",
                tradingsymbol="MARUTI26JUL14300CE", spot_sl=14150.0, target_pts=42.5,
            )
        assert ok is False
