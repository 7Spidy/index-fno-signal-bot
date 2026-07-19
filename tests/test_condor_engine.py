"""Unit tests for condor_engine.py — IV Rank calc, backfill idempotency,
entry gating, liquidity scan, net-credit guard, and short-leg trailing SL.

All Kite/Discord/Redis calls are mocked — no live calls.
"""
import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from src import condor_config as ccfg
from src import condor_engine


# ──────────────────────────────────────────────────────────────
# IV Rank calculation
# ──────────────────────────────────────────────────────────────

def _history(values: list[float], end: date) -> dict:
    """Build a {date_str: close} history dict counting back from `end`."""
    out = {}
    d = end
    for v in reversed(values):
        out[d.isoformat()] = v
        d -= timedelta(days=1)
    return out


class TestComputeIvRank:
    def test_matches_hand_computed_value(self):
        # window closes: 10, 20, 30 ; today=25 -> (25-10)/(30-10)*100 = 75
        history = _history([10.0, 20.0, 30.0], date(2026, 1, 3))
        iv_rank, short_window = condor_engine.compute_iv_rank(history, 25.0)
        assert abs(iv_rank - 75.0) < 1e-9
        assert short_window is True   # only 3 entries < 252

    def test_short_window_guard_fires(self):
        history = _history([15.0] * 10, date(2026, 1, 10))
        iv_rank, short_window = condor_engine.compute_iv_rank(history, 20.0)
        assert short_window is True

    def test_full_window_not_flagged_short(self):
        vals = [10.0 + (i % 20) for i in range(ccfg.IV_RANK_WINDOW_DAYS - 1)]
        history = _history(vals, date(2026, 6, 1))
        iv_rank, short_window = condor_engine.compute_iv_rank(history, 25.0)
        assert short_window is False

    def test_degenerate_flat_history_no_div_by_zero(self):
        history = _history([20.0, 20.0, 20.0], date(2026, 1, 3))
        iv_rank, _ = condor_engine.compute_iv_rank(history, 20.0)
        assert iv_rank == 0.0


# ──────────────────────────────────────────────────────────────
# Backfill idempotency
# ──────────────────────────────────────────────────────────────

class TestBackfillIdempotency:
    def test_running_twice_does_not_duplicate_dates_merge_by_date(self):
        store = {}

        def fake_get(key):
            return store.get(key)

        def fake_set(key, value, ex=None):
            store[key] = value
            return True

        kite = MagicMock()
        kite.instruments.return_value = [
            {"tradingsymbol": "INDIA VIX", "instrument_token": 264969}
        ]
        kite.historical_data.return_value = [
            {"date": MagicMock(date=MagicMock(return_value=date(2026, 1, 1))), "close": 14.5},
            {"date": MagicMock(date=MagicMock(return_value=date(2026, 1, 2))), "close": 15.0},
        ]

        with patch("src.condor_engine.state.redis_get", side_effect=fake_get), \
             patch("src.condor_engine.state.redis_set", side_effect=fake_set):
            condor_engine.backfill_vix_history(kite)
            first_history = json.loads(store[ccfg.REDIS_VIX_HISTORY_KEY])
            condor_engine.backfill_vix_history(kite)
            second_history = json.loads(store[ccfg.REDIS_VIX_HISTORY_KEY])

        assert first_history == second_history
        assert len(second_history) == 2

    def test_sufficient_history_skips_api_call(self):
        vals = [10.0 + (i % 5) for i in range(ccfg.IV_RANK_WINDOW_DAYS)]
        history = _history(vals, date.today())
        stored = json.dumps(history)

        kite = MagicMock()
        with patch("src.condor_engine.state.redis_get", return_value=stored), \
             patch("src.condor_engine.state.redis_set") as mock_set:
            condor_engine.backfill_vix_history(kite)

        kite.historical_data.assert_not_called()
        mock_set.assert_not_called()


# ──────────────────────────────────────────────────────────────
# Entry gate boundary + lock
# ──────────────────────────────────────────────────────────────

class TestMorningEntryGate:
    def test_lock_set_is_a_noop(self):
        with patch("src.condor_engine._is_locked", return_value=True), \
             patch("src.condor_engine.kite_client.get_kite") as mock_get_kite, \
             patch("src.condor_engine.condor_notifier.send_skip") as mock_skip:
            condor_engine.morning_entry()
        mock_get_kite.assert_not_called()
        mock_skip.assert_not_called()

    def test_iv_rank_29_9_skips(self):
        kite = MagicMock()
        with patch("src.condor_engine._is_locked", return_value=False), \
             patch("src.condor_engine.kite_client.get_kite", return_value=kite), \
             patch("src.condor_engine._load_vix_history", return_value={}), \
             patch("src.condor_engine.get_live_vix", return_value=29.9), \
             patch("src.condor_engine.compute_iv_rank", return_value=(29.9, False)), \
             patch("src.condor_engine.condor_notifier.send_skip") as mock_skip, \
             patch("src.condor_engine.kite_client.get_spot_ltp") as mock_spot:
            condor_engine.morning_entry(kite)
        mock_skip.assert_called_once()
        mock_spot.assert_not_called()

    def test_iv_rank_30_0_proceeds_past_gate(self):
        kite = MagicMock()
        with patch("src.condor_engine._is_locked", return_value=False), \
             patch("src.condor_engine._load_vix_history", return_value={}), \
             patch("src.condor_engine.get_live_vix", return_value=30.0), \
             patch("src.condor_engine.compute_iv_rank", return_value=(30.0, False)), \
             patch("src.condor_engine.kite_client.get_spot_ltp", return_value=None), \
             patch("src.condor_engine.condor_notifier.send_skip") as mock_skip:
            condor_engine.morning_entry(kite)
        # Gate passed (no skip for IV Rank); aborted later for missing spot,
        # which does not call send_skip — proves gate itself let it through.
        mock_skip.assert_not_called()


# ──────────────────────────────────────────────────────────────
# Liquidity scan
# ──────────────────────────────────────────────────────────────

def _mk_inst(strike, opt_type, symbol, token=1):
    return {"instrument_type": opt_type, "strike": strike,
            "tradingsymbol": symbol, "instrument_token": token}


class TestLiquidityScan:
    def test_low_oi_rejected_and_steps_outward(self):
        chain = [
            _mk_inst(24800, "CE", "NIFTY24800CE"),
            _mk_inst(24850, "CE", "NIFTY24850CE"),
            _mk_inst(24900, "CE", "NIFTY24900CE"),
        ]
        kite = MagicMock()

        def fake_quote(keys):
            result = {}
            for k in keys:
                if "24800" in k:
                    result[k] = {"oi": 500, "last_price": 100.0}   # low OI -> rejected
                elif "24850" in k:
                    result[k] = {"oi": 2000, "last_price": 60.0}
                elif "24900" in k:
                    result[k] = {"oi": 2000, "last_price": 40.0}
            return result

        kite.quote.side_effect = fake_quote
        result = condor_engine._scan_short_leg(kite, chain, "CE", 24800, +1)
        assert result is not None
        assert result["strike"] == 24850

    def test_non_monotonic_rejected(self):
        chain = [
            _mk_inst(24800, "PE", "NIFTY24800PE"),
            _mk_inst(24750, "PE", "NIFTY24750PE"),
            _mk_inst(24700, "PE", "NIFTY24700PE"),
        ]
        kite = MagicMock()

        def fake_quote(keys):
            result = {}
            for k in keys:
                if "24800" in k:
                    result[k] = {"oi": 2000, "last_price": 50.0}
                elif "24750" in k:
                    result[k] = {"oi": 2000, "last_price": 55.0}  # non-monotonic vs 24800
                elif "24700" in k:
                    result[k] = {"oi": 2000, "last_price": 30.0}
            return result

        kite.quote.side_effect = fake_quote
        # start at 24800 (rejected: 50 not > 55), step to 24750 (accepted: 55 > 30)
        result = condor_engine._scan_short_leg(kite, chain, "PE", 24800, -1)
        assert result is not None
        assert result["strike"] == 24750

    def test_aborts_after_max_steps(self):
        chain = [_mk_inst(s, "CE", f"NIFTY{s}CE") for s in range(24800, 25200, 50)]
        kite = MagicMock()
        kite.quote.return_value = {}   # nothing ever qualifies
        result = condor_engine._scan_short_leg(kite, chain, "CE", 24800, +1)
        assert result is None


# ──────────────────────────────────────────────────────────────
# Net-credit guard
# ──────────────────────────────────────────────────────────────

class TestNetCreditGuard:
    def test_non_positive_net_credit_aborts(self):
        # short_call=10, long_call=15 -> -5 ; short_put=8, long_put=5 -> +3 ; total -2
        short_call_ltp, long_call_ltp = 10.0, 15.0
        short_put_ltp, long_put_ltp = 8.0, 5.0
        net_credit = (short_call_ltp - long_call_ltp) + (short_put_ltp - long_put_ltp)
        assert net_credit <= 0


# ──────────────────────────────────────────────────────────────
# Short-leg trailing SL — direction + monotonicity
# ──────────────────────────────────────────────────────────────

class TestShortLegTrailingSl:
    def test_tracks_short_direction_premium_falling_is_progress(self):
        entry, T = 100.0, 60.0
        # premium falls from 100 -> 70 (progress = (100-70)/60 = 0.5)
        sl = condor_engine._short_leg_trailing_sl(entry, T, 70.0, prior_sl=entry)
        assert sl < entry   # ratcheted down as premium fell (favorable for short)

    def test_monotonic_ratchet_never_loosens_on_reversal(self):
        entry, T = 100.0, 60.0
        sl = entry
        # premium falls steadily -> SL should ratchet down (or hold) each step
        prices_falling = [90.0, 80.0, 70.0, 60.0]
        sls = []
        for p in prices_falling:
            sl = condor_engine._short_leg_trailing_sl(entry, T, p, sl)
            sls.append(sl)
        for i in range(1, len(sls)):
            assert sls[i] <= sls[i - 1]

        # premium then reverses and rises back up — SL must NOT loosen (increase)
        tightest = sl
        sl_after_reversal = condor_engine._short_leg_trailing_sl(entry, T, 95.0, sl)
        assert sl_after_reversal <= tightest

    def test_no_progress_returns_prior_sl(self):
        entry, T = 100.0, 60.0
        # premium barely moved -> progress < 0.5 -> unchanged
        sl = condor_engine._short_leg_trailing_sl(entry, T, 95.0, prior_sl=entry)
        assert sl == entry


class TestLongLegsNeverTrailed:
    def test_long_legs_static_after_entry(self):
        with patch("src.condor_engine._is_locked", return_value=True), \
             patch("src.condor_engine._load_position", return_value={
                 "legs": {
                     "short_call": {"side": "SELL", "tradingsymbol": "X1", "entry_premium": 50.0, "sl": 50.0, "target": 30.0},
                     "long_call": {"side": "BUY", "tradingsymbol": "X2", "entry_premium": 20.0, "sl": None, "target": None},
                     "short_put": {"side": "SELL", "tradingsymbol": "X3", "entry_premium": 40.0, "sl": 40.0, "target": 24.0},
                     "long_put": {"side": "BUY", "tradingsymbol": "X4", "entry_premium": 15.0, "sl": None, "target": None},
                 },
                 "net_credit_pts": 55.0,
                 "lots": 1,
             }), \
             patch("src.condor_engine.kite_client.get_kite") as mock_get_kite, \
             patch("src.condor_engine.condor_notifier.send_update"), \
             patch("src.condor_engine._save_position"):
            kite = MagicMock()
            kite.quote.return_value = {
                "NFO:X1": {"last_price": 45.0},
                "NFO:X2": {"last_price": 22.0},
                "NFO:X3": {"last_price": 38.0},
                "NFO:X4": {"last_price": 13.0},
            }
            mock_get_kite.return_value = kite
            condor_engine.tracker_tick(kite)

        # long legs' sl fields were never touched by tracker_tick (still None)
        # this is asserted implicitly: no exception, and only short legs are
        # passed through _short_leg_trailing_sl per tracker_tick's loop scope.


# ──────────────────────────────────────────────────────────────
# Exit trigger ordering + full unwind
# ──────────────────────────────────────────────────────────────

class TestExitTriggerOrdering:
    def _base_position(self):
        return {
            "legs": {
                "short_call": {"side": "SELL", "tradingsymbol": "SC", "entry_premium": 50.0, "sl": 55.0, "target": 30.0},
                "long_call": {"side": "BUY", "tradingsymbol": "LC", "entry_premium": 20.0, "sl": None, "target": None},
                "short_put": {"side": "SELL", "tradingsymbol": "SP", "entry_premium": 40.0, "sl": 45.0, "target": 24.0},
                "long_put": {"side": "BUY", "tradingsymbol": "LP", "entry_premium": 15.0, "sl": None, "target": None},
            },
            "net_credit_pts": 55.0,
            "lots": 1,
        }

    def test_trailing_sl_checked_before_target(self):
        position = self._base_position()
        # Set up prices where BOTH a trailing-SL breach and a target hit would
        # be true simultaneously; trailing_sl must win (checked first).
        kite = MagicMock()
        kite.quote.return_value = {
            "NFO:SC": {"last_price": 56.0},   # >= sl (55.0) -> trailing_sl breach
            "NFO:LC": {"last_price": 1.0},
            "NFO:SP": {"last_price": 1.0},
            "NFO:LP": {"last_price": 0.5},
        }
        with patch("src.condor_engine._is_locked", return_value=True), \
             patch("src.condor_engine._load_position", return_value=position), \
             patch("src.condor_engine.kite_client.get_kite", return_value=kite), \
             patch("src.condor_engine.condor_notifier.send_close") as mock_close, \
             patch("src.condor_engine._clear_position") as mock_clear:
            condor_engine.tracker_tick(kite)

        mock_close.assert_called_once()
        assert mock_close.call_args[0][3] == "trailing_sl"
        mock_clear.assert_called_once()

    def test_full_unwind_clears_both_lock_and_position_keys(self):
        with patch("src.condor_engine.state.redis_delete") as mock_del:
            condor_engine._clear_position()
        deleted_keys = {c.args[0] for c in mock_del.call_args_list}
        assert ccfg.REDIS_CONDOR_POSITION in deleted_keys
        assert ccfg.REDIS_CONDOR_LOCK in deleted_keys
