"""Unit tests for position_tracker.py — ladder function, SL invariants, and
the pull-based discovery / confirm / exit flow."""
from datetime import datetime
from unittest.mock import MagicMock, patch

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
# New sighting (first heartbeat) — pending, no alert
# ──────────────────────────────────────────────────────────────

class TestNewSighting:
    def test_creates_pending_state_confirm_count_1_no_alert(self):
        pos = {"tradingsymbol": "NIFTY26JUN24600CE", "quantity": 75, "average_price": 120.5}
        with patch("src.position_tracker._save_position") as mock_save, \
             patch("src.position_tracker.trade_notifier.send_position_detected") as mock_alert:
            position_tracker._handle_new_sighting("NIFTY26JUN24600CE", pos)

        mock_alert.assert_not_called()
        mock_save.assert_called_once()
        saved_symbol, saved_data = mock_save.call_args[0]
        assert saved_symbol == "NIFTY26JUN24600CE"
        assert saved_data["confirm_count"] == 1
        assert saved_data["direction"] == "CE"
        assert saved_data["qty"] == 75
        assert saved_data["sl"] is None
        assert saved_data["target_t"] is None

    def test_unrecognised_direction_is_skipped(self):
        pos = {"tradingsymbol": "NIFTY26JUNFUT", "quantity": 75, "average_price": 100.0}
        with patch("src.position_tracker._save_position") as mock_save:
            position_tracker._handle_new_sighting("NIFTY26JUNFUT", pos)
        mock_save.assert_not_called()


# ──────────────────────────────────────────────────────────────
# Confirm (second heartbeat) — matches alert intent, posts alert
# ──────────────────────────────────────────────────────────────

class TestConfirmFlow:
    def _pending(self):
        return {
            "tradingsymbol": "NIFTY26JUN24600CE", "instrument": "NIFTY", "direction": "CE",
            "entry_price": 120.5, "sl": None, "target_t": None, "entry_alert_ts": None,
            "discovered_at": "2026-06-30T09:16:00+05:30", "sl_ladder_stage": None,
            "qty": 75, "confirm_count": 1, "action_alerts_sent": 0, "action_alerts_acked": 0,
        }

    def test_confirm_with_matching_intent_posts_alert_and_advances_confirm_count(self):
        existing = self._pending()
        pos = {"tradingsymbol": "NIFTY26JUN24600CE", "quantity": 75, "average_price": 121.0}
        intent = {
            "instrument": "NIFTY", "spot_risk_pts": 20.0, "target_rr": 1.5,
            "spot_sl": 24480.0, "ts": "2026-06-30T09:15:30+00:00",
        }
        mock_kite = MagicMock()
        with patch("src.position_tracker._load_intent", return_value=intent), \
             patch("src.position_tracker._save_position") as mock_save, \
             patch("src.position_tracker.trade_notifier.send_position_detected") as mock_alert:
            position_tracker._handle_confirm(mock_kite, "NIFTY26JUN24600CE", pos, existing)

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        assert kwargs["sl"] == 24480.0
        assert kwargs["target_t"] == 30.0   # 20 * 1.5
        assert kwargs["entry_price"] == 121.0
        assert kwargs["qty"] == 75

        saved_symbol, saved_data = mock_save.call_args[0]
        assert saved_data["confirm_count"] == 2
        assert saved_data["sl"] == 24480.0
        assert saved_data["sl_ladder_stage"] == 24480.0
        assert saved_data["target_t"] == 30.0
        assert saved_data["entry_alert_ts"] == "2026-06-30T09:15:30+00:00"

    def test_confirm_without_intent_falls_back_to_kite_sl_or_entry(self):
        existing = self._pending()
        pos = {"tradingsymbol": "NIFTY26JUN24600CE", "quantity": 75, "average_price": 120.5}
        mock_kite = MagicMock()
        with patch("src.position_tracker._load_intent", return_value=None), \
             patch("src.position_tracker._get_kite_sl_for", return_value=None), \
             patch("src.position_tracker._save_position") as mock_save, \
             patch("src.position_tracker.trade_notifier.send_position_detected") as mock_alert:
            position_tracker._handle_confirm(mock_kite, "NIFTY26JUN24600CE", pos, existing)

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        assert kwargs["sl"] is None          # unavailable — no intent, no Kite SL order
        assert kwargs["target_t"] is None

        saved_symbol, saved_data = mock_save.call_args[0]
        assert saved_data["confirm_count"] == 2
        assert saved_data["sl"] == 120.5     # falls back to entry so ladder has a floor
        assert saved_data["target_t"] is None

    def test_confirm_posts_exactly_one_alert(self):
        existing = self._pending()
        pos = {"tradingsymbol": "NIFTY26JUN24600CE", "quantity": 75, "average_price": 120.5}
        mock_kite = MagicMock()
        with patch("src.position_tracker._load_intent", return_value=None), \
             patch("src.position_tracker._get_kite_sl_for", return_value=None), \
             patch("src.position_tracker._save_position"), \
             patch("src.position_tracker.trade_notifier.send_position_detected") as mock_alert, \
             patch("src.position_tracker.trade_notifier.send_fyi") as mock_fyi, \
             patch("src.position_tracker.trade_notifier.send_action") as mock_action:
            position_tracker._handle_confirm(mock_kite, "NIFTY26JUN24600CE", pos, existing)
        assert mock_alert.call_count == 1
        mock_fyi.assert_not_called()
        mock_action.assert_not_called()


# ──────────────────────────────────────────────────────────────
# Ongoing tracking — qty increase (averaging) / decrease (partial exit)
# ──────────────────────────────────────────────────────────────

class TestQtyChangeHandling:
    def _confirmed(self, qty=75, T=30.0, sl_ladder_stage=150.0):
        return {
            "tradingsymbol": "NIFTY26JUN24600CE", "instrument": "NIFTY", "direction": "CE",
            "entry_price": 120.5, "sl": 24480.0, "target_t": T, "entry_alert_ts": "x",
            "discovered_at": "x", "sl_ladder_stage": sl_ladder_stage,
            "qty": qty, "confirm_count": 2, "action_alerts_sent": 0, "action_alerts_acked": 0,
        }

    def _mock_kite(self, ltp=135.0):
        mock_kite = MagicMock()
        mock_kite.ltp.return_value = {"NFO:NIFTY26JUN24600CE": {"last_price": ltp}}
        mock_kite.orders.return_value = []
        return mock_kite

    def test_qty_increase_updates_entry_price_keeps_sl_and_t(self):
        existing = self._confirmed(qty=75)
        pos = {"tradingsymbol": "NIFTY26JUN24600CE", "quantity": 150, "average_price": 130.0}
        mock_kite = self._mock_kite()
        with patch("src.position_tracker._save_position") as mock_save, \
             patch("src.position_tracker.trade_notifier.send_fyi"), \
             patch("src.position_tracker.trade_notifier.send_partial_exit") as mock_partial, \
             patch("src.position_tracker._get_rsi_snapshot", return_value=None):
            position_tracker._handle_ongoing(
                mock_kite, "NIFTY26JUN24600CE", pos, existing, datetime(2026, 6, 30, 9, 15)
            )

        mock_partial.assert_not_called()
        saved_symbol, saved_data = mock_save.call_args[0]
        assert saved_data["entry_price"] == 130.0
        assert saved_data["qty"] == 150
        assert saved_data["target_t"] == 30.0   # T unchanged — permanently fixed at entry

    def test_qty_decrease_posts_partial_exit_note_keeps_ladder(self):
        existing = self._confirmed(qty=150)
        pos = {"tradingsymbol": "NIFTY26JUN24600CE", "quantity": 75, "average_price": 130.0}
        mock_kite = self._mock_kite()
        with patch("src.position_tracker._save_position") as mock_save, \
             patch("src.position_tracker.trade_notifier.send_fyi"), \
             patch("src.position_tracker.trade_notifier.send_partial_exit") as mock_partial, \
             patch("src.position_tracker._get_rsi_snapshot", return_value=None):
            position_tracker._handle_ongoing(
                mock_kite, "NIFTY26JUN24600CE", pos, existing, datetime(2026, 6, 30, 9, 15)
            )

        mock_partial.assert_called_once_with("NIFTY", "CE", "NIFTY26JUN24600CE", 150, 75)
        saved_symbol, saved_data = mock_save.call_args[0]
        assert saved_data["qty"] == 75

    def test_qty_unchanged_no_partial_exit_note(self):
        existing = self._confirmed(qty=75)
        pos = {"tradingsymbol": "NIFTY26JUN24600CE", "quantity": 75, "average_price": 120.5}
        mock_kite = self._mock_kite()
        with patch("src.position_tracker._save_position"), \
             patch("src.position_tracker.trade_notifier.send_fyi"), \
             patch("src.position_tracker.trade_notifier.send_partial_exit") as mock_partial, \
             patch("src.position_tracker._get_rsi_snapshot", return_value=None):
            position_tracker._handle_ongoing(
                mock_kite, "NIFTY26JUN24600CE", pos, existing, datetime(2026, 6, 30, 9, 15)
            )
        mock_partial.assert_not_called()


# ──────────────────────────────────────────────────────────────
# Exit detection — qty -> 0 transition
# ──────────────────────────────────────────────────────────────

class TestGetKiteExitPrice:
    def test_averages_multiple_sell_fills(self):
        mock_kite = MagicMock()
        mock_kite.orders.return_value = [
            {"tradingsymbol": "X", "transaction_type": "SELL", "status": "COMPLETE",
             "filled_quantity": 50, "average_price": 100.0},
            {"tradingsymbol": "X", "transaction_type": "SELL", "status": "COMPLETE",
             "filled_quantity": 25, "average_price": 106.0},
            {"tradingsymbol": "X", "transaction_type": "BUY", "status": "COMPLETE",
             "filled_quantity": 75, "average_price": 80.0},
        ]
        price = position_tracker._get_kite_exit_price(mock_kite, "X")
        assert abs(price - ((100 * 50 + 106 * 25) / 75)) < 1e-9

    def test_no_sell_fills_returns_none(self):
        mock_kite = MagicMock()
        mock_kite.orders.return_value = []
        assert position_tracker._get_kite_exit_price(mock_kite, "X") is None

    def test_orders_call_failure_returns_none(self):
        mock_kite = MagicMock()
        mock_kite.orders.side_effect = Exception("network error")
        assert position_tracker._get_kite_exit_price(mock_kite, "X") is None


class TestExitDetection:
    def _confirmed_existing(self):
        return {
            "tradingsymbol": "NIFTY26JUN24600CE", "instrument": "NIFTY", "direction": "CE",
            "entry_price": 120.5, "sl": 24480.0, "target_t": 30.0, "entry_alert_ts": "x",
            "discovered_at": "x", "sl_ladder_stage": 150.0,
            "qty": 75, "confirm_count": 2, "action_alerts_sent": 4, "action_alerts_acked": 3,
        }

    def test_exit_uses_get_orders_average_price_and_labels_ladder_driven(self):
        existing = self._confirmed_existing()
        mock_kite = MagicMock()
        mock_kite.orders.return_value = [
            {"tradingsymbol": "NIFTY26JUN24600CE", "transaction_type": "SELL",
             "status": "COMPLETE", "filled_quantity": 75, "average_price": 150.0},
        ]
        with patch("src.position_tracker._delete_position") as mock_delete, \
             patch("src.position_tracker.trade_notifier.send_exit_summary") as mock_summary:
            position_tracker._finalize_exit(mock_kite, "NIFTY26JUN24600CE", existing, None)

        mock_summary.assert_called_once()
        kwargs = mock_summary.call_args.kwargs
        assert kwargs["exit_price"] == 150.0
        assert kwargs["exit_type"] == "Ladder SL"
        assert abs(kwargs["pnl"] - (150.0 - 120.5) * 75) < 1e-6
        mock_delete.assert_called_once_with("NIFTY26JUN24600CE")

    def test_exit_price_far_from_ladder_labelled_manual(self):
        existing = self._confirmed_existing()
        existing["sl_ladder_stage"] = 200.0
        mock_kite = MagicMock()
        mock_kite.orders.return_value = [
            {"tradingsymbol": "NIFTY26JUN24600CE", "transaction_type": "SELL",
             "status": "COMPLETE", "filled_quantity": 75, "average_price": 100.0},
        ]
        with patch("src.position_tracker._delete_position"), \
             patch("src.position_tracker.trade_notifier.send_exit_summary") as mock_summary:
            position_tracker._finalize_exit(mock_kite, "NIFTY26JUN24600CE", existing, None)
        kwargs = mock_summary.call_args.kwargs
        assert kwargs["exit_type"] == "Manual / untracked flatten"

    def test_untracked_exit_still_always_posts(self):
        """Guards against silently swallowing the untracked-exit case — must
        always post to Discord even with no ladder stage and no order fills."""
        existing = self._confirmed_existing()
        existing["sl_ladder_stage"] = None
        mock_kite = MagicMock()
        mock_kite.orders.return_value = []
        with patch("src.position_tracker._delete_position"), \
             patch("src.position_tracker.trade_notifier.send_exit_summary") as mock_summary:
            position_tracker._finalize_exit(mock_kite, "NIFTY26JUN24600CE", existing, None)
        mock_summary.assert_called_once()
        assert mock_summary.call_args.kwargs["exit_type"] == "Manual / untracked flatten"

    def test_exit_deletes_position_key(self):
        existing = self._confirmed_existing()
        mock_kite = MagicMock()
        mock_kite.orders.return_value = []
        with patch("src.position_tracker._delete_position") as mock_delete, \
             patch("src.position_tracker.trade_notifier.send_exit_summary"):
            position_tracker._finalize_exit(mock_kite, "NIFTY26JUN24600CE", existing, None)
        mock_delete.assert_called_once_with("NIFTY26JUN24600CE")


class TestDisappearedBeforeConfirm:
    def test_pending_position_vanishing_is_discarded_silently(self):
        pending = {
            "tradingsymbol": "NIFTY26JUN24600CE", "instrument": "NIFTY", "direction": "CE",
            "entry_price": 120.5, "sl": None, "target_t": None, "entry_alert_ts": None,
            "discovered_at": "x", "sl_ladder_stage": None,
            "qty": 75, "confirm_count": 1, "action_alerts_sent": 0, "action_alerts_acked": 0,
        }
        mock_kite = MagicMock()
        with patch("src.position_tracker._load_position", return_value=pending), \
             patch("src.position_tracker._delete_position") as mock_delete, \
             patch("src.position_tracker.trade_notifier.send_exit_summary") as mock_summary:
            position_tracker._handle_disappeared(mock_kite, "NIFTY26JUN24600CE", None)

        mock_delete.assert_called_once_with("NIFTY26JUN24600CE")
        mock_summary.assert_not_called()

    def test_confirmed_position_vanishing_triggers_full_exit_flow(self):
        confirmed = {
            "tradingsymbol": "NIFTY26JUN24600CE", "instrument": "NIFTY", "direction": "CE",
            "entry_price": 120.5, "sl": 24480.0, "target_t": 30.0, "entry_alert_ts": "x",
            "discovered_at": "x", "sl_ladder_stage": 150.0,
            "qty": 75, "confirm_count": 2, "action_alerts_sent": 0, "action_alerts_acked": 0,
        }
        mock_kite = MagicMock()
        mock_kite.orders.return_value = []
        with patch("src.position_tracker._load_position", return_value=confirmed), \
             patch("src.position_tracker._delete_position") as mock_delete, \
             patch("src.position_tracker.trade_notifier.send_exit_summary") as mock_summary:
            position_tracker._handle_disappeared(mock_kite, "NIFTY26JUN24600CE", None)

        mock_summary.assert_called_once()
        mock_delete.assert_called_once_with("NIFTY26JUN24600CE")

    def test_index_only_entry_with_no_position_data_is_pruned(self):
        mock_kite = MagicMock()
        with patch("src.position_tracker._load_position", return_value=None), \
             patch("src.position_tracker._remove_from_index") as mock_remove, \
             patch("src.position_tracker.trade_notifier.send_exit_summary") as mock_summary:
            position_tracker._handle_disappeared(mock_kite, "STALE_SYMBOL", None)
        mock_remove.assert_called_once_with("STALE_SYMBOL")
        mock_summary.assert_not_called()
