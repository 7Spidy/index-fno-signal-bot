"""Unit tests for the sub-minute heartbeat loop in position_tracker.py
(2026-07-23 change spec: 15s-resolution sampling within a single 1-minute
dispatch). Covers loop control in main(), exception isolation, wall-clock
budget, env-driven config parsing, the process-local RSI cache, the Redis
write gate on sl_ladder_stage, and the live-LTP override for Discord.

All tests patch time.sleep so the suite stays fast — no real waiting.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

from src import position_tracker
from src.position_tracker import IST, _bucket_5m, _elapsed_since_job_start, _loop_config


# ──────────────────────────────────────────────────────────────
# Loop control — main()
# ──────────────────────────────────────────────────────────────

class TestLoopControl:
    def test_four_passes_all_open_runs_four_times(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        monkeypatch.delenv("TRACKER_JOB_START_EPOCH", raising=False)
        statuses = [{"open_count": 1, "is_eod": False}] * 4
        with (
            patch.object(position_tracker, "run_heartbeat", side_effect=statuses) as mock_hb,
            patch.object(position_tracker.time, "sleep") as mock_sleep,
        ):
            position_tracker.main()
        assert mock_hb.call_count == 4
        assert mock_sleep.call_count == 3

    def test_subloops_1_runs_once_no_sleep(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "1")
        with (
            patch.object(position_tracker, "run_heartbeat",
                         return_value={"open_count": 1, "is_eod": False}) as mock_hb,
            patch.object(position_tracker.time, "sleep") as mock_sleep,
        ):
            position_tracker.main()
        mock_hb.assert_called_once()
        mock_sleep.assert_not_called()

    def test_pass1_open_count_zero_breaks_after_one(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        with (
            patch.object(position_tracker, "run_heartbeat",
                         return_value={"open_count": 0, "is_eod": False}) as mock_hb,
            patch.object(position_tracker.time, "sleep") as mock_sleep,
        ):
            position_tracker.main()
        mock_hb.assert_called_once()
        mock_sleep.assert_not_called()

    def test_pass1_is_eod_breaks_after_one(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        with (
            patch.object(position_tracker, "run_heartbeat",
                         return_value={"open_count": 1, "is_eod": True}) as mock_hb,
            patch.object(position_tracker.time, "sleep") as mock_sleep,
        ):
            position_tracker.main()
        mock_hb.assert_called_once()
        mock_sleep.assert_not_called()

    def test_pass2_open_count_zero_stops_after_two(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        statuses = [
            {"open_count": 1, "is_eod": False},
            {"open_count": 0, "is_eod": False},
        ]
        with (
            patch.object(position_tracker, "run_heartbeat", side_effect=statuses) as mock_hb,
            patch.object(position_tracker.time, "sleep"),
        ):
            position_tracker.main()
        assert mock_hb.call_count == 2

    def test_no_trailing_sleep_after_final_pass(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        statuses = [{"open_count": 1, "is_eod": False}] * 4
        with (
            patch.object(position_tracker, "run_heartbeat", side_effect=statuses),
            patch.object(position_tracker.time, "sleep") as mock_sleep,
        ):
            position_tracker.main()
        assert mock_sleep.call_count == 3


# ──────────────────────────────────────────────────────────────
# Exception isolation
# ──────────────────────────────────────────────────────────────

class TestExceptionIsolation:
    def test_pass2_raises_passes_3_and_4_still_run(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")

        def side_effect(*args, **kwargs):
            raise AssertionError("unused")

        calls = {"n": 0}

        def hb():
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return {"open_count": 1, "is_eod": False}

        with (
            patch.object(position_tracker, "run_heartbeat", side_effect=hb) as mock_hb,
            patch.object(position_tracker.time, "sleep"),
        ):
            position_tracker.main()  # must not raise
        assert mock_hb.call_count == 4

    def test_every_pass_raises_main_returns_cleanly(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        with (
            patch.object(position_tracker, "run_heartbeat", side_effect=RuntimeError("boom")) as mock_hb,
            patch.object(position_tracker.time, "sleep"),
        ):
            position_tracker.main()  # must not raise
        assert mock_hb.call_count == 4


# ──────────────────────────────────────────────────────────────
# Budget
# ──────────────────────────────────────────────────────────────

class TestBudget:
    def test_budget_20_interval_15_only_two_passes(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        monkeypatch.setenv("TRACKER_SUBLOOP_SECS", "15")
        monkeypatch.setenv("TRACKER_LOOP_BUDGET_SECS", "20")
        monkeypatch.delenv("TRACKER_JOB_START_EPOCH", raising=False)
        statuses = [{"open_count": 1, "is_eod": False}] * 4
        with (
            patch.object(position_tracker, "run_heartbeat", side_effect=statuses) as mock_hb,
            patch.object(position_tracker.time, "sleep"),
        ):
            position_tracker.main()
        assert mock_hb.call_count == 2

    def test_job_start_50s_ago_budget_55_only_pass_one(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "4")
        monkeypatch.setenv("TRACKER_LOOP_BUDGET_SECS", "55")
        monkeypatch.setenv("TRACKER_JOB_START_EPOCH", str(__import__("time").time() - 50))
        statuses = [{"open_count": 1, "is_eod": False}] * 4
        with (
            patch.object(position_tracker, "run_heartbeat", side_effect=statuses) as mock_hb,
            patch.object(position_tracker.time, "sleep") as mock_sleep,
        ):
            position_tracker.main()
        assert mock_hb.call_count == 1
        mock_sleep.assert_not_called()

    def test_elapsed_since_job_start_absent_returns_zero(self, monkeypatch):
        monkeypatch.delenv("TRACKER_JOB_START_EPOCH", raising=False)
        assert _elapsed_since_job_start() == 0.0

    def test_elapsed_since_job_start_malformed_returns_zero(self, monkeypatch):
        monkeypatch.setenv("TRACKER_JOB_START_EPOCH", "abc")
        assert _elapsed_since_job_start() == 0.0


# ──────────────────────────────────────────────────────────────
# Config parsing
# ──────────────────────────────────────────────────────────────

class TestLoopConfig:
    def test_unset_env_returns_defaults(self, monkeypatch):
        monkeypatch.delenv("TRACKER_SUBLOOPS", raising=False)
        monkeypatch.delenv("TRACKER_SUBLOOP_SECS", raising=False)
        monkeypatch.delenv("TRACKER_LOOP_BUDGET_SECS", raising=False)
        assert _loop_config() == (4, 15.0, 55.0)

    def test_subloops_zero_clamped_to_one(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "0")
        subloops, _, _ = _loop_config()
        assert subloops == 1

    def test_malformed_values_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("TRACKER_SUBLOOPS", "not-a-number")
        monkeypatch.setenv("TRACKER_SUBLOOP_SECS", "also-bad")
        monkeypatch.setenv("TRACKER_LOOP_BUDGET_SECS", "still-bad")
        assert _loop_config() == (4, 15.0, 55.0)


# ──────────────────────────────────────────────────────────────
# RSI cache
# ──────────────────────────────────────────────────────────────

class _FrozenDatetime(datetime):
    _now = datetime(2026, 7, 23, 10, 3, tzinfo=IST)

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _fake_df():
    import numpy as np
    import pandas as pd
    ts = pd.date_range("2026-07-23 09:15", periods=20, freq="5min")
    return pd.DataFrame({
        "timestamp": ts,
        "open":  np.linspace(100, 120, 20),
        "high":  np.linspace(101, 121, 20),
        "low":   np.linspace(99, 119, 20),
        "close": np.linspace(100, 120, 20),
        "volume": np.full(20, 1000.0),
    })


class TestRsiCache:
    def _token_map(self):
        return '{"NIFTY": {"token": 111}, "BANKNIFTY": {"token": 222}}'

    def test_four_calls_same_bucket_fetches_once(self):
        cache: dict = {}
        with (
            patch.object(position_tracker, "datetime", _FrozenDatetime),
            patch("src.state.redis_get", return_value=self._token_map()),
            patch("src.kite_client.fetch_ohlcv", return_value=_fake_df()) as mock_fetch,
        ):
            for _ in range(4):
                result = position_tracker._get_rsi_snapshot(
                    "NIFTY", datetime(2026, 7, 23, 9, 15), cache=cache)
                assert result is not None
        assert mock_fetch.call_count == 1

    def test_bucket_boundary_crossed_refetches(self):
        class _T1(_FrozenDatetime):
            _now = datetime(2026, 7, 23, 10, 3, tzinfo=IST)

        class _T2(_FrozenDatetime):
            _now = datetime(2026, 7, 23, 10, 6, tzinfo=IST)

        cache: dict = {}
        with (
            patch("src.state.redis_get", return_value=self._token_map()),
            patch("src.kite_client.fetch_ohlcv", return_value=_fake_df()) as mock_fetch,
        ):
            with patch.object(position_tracker, "datetime", _T1):
                position_tracker._get_rsi_snapshot("NIFTY", datetime(2026, 7, 23, 9, 15), cache=cache)
            with patch.object(position_tracker, "datetime", _T2):
                position_tracker._get_rsi_snapshot("NIFTY", datetime(2026, 7, 23, 9, 15), cache=cache)
        assert mock_fetch.call_count == 2

    def test_two_instruments_same_bucket_two_fetches_no_cross_contamination(self):
        cache: dict = {}
        with (
            patch.object(position_tracker, "datetime", _FrozenDatetime),
            patch("src.state.redis_get", return_value=self._token_map()),
            patch("src.kite_client.fetch_ohlcv", return_value=_fake_df()) as mock_fetch,
        ):
            position_tracker._get_rsi_snapshot("NIFTY", datetime(2026, 7, 23, 9, 15), cache=cache)
            position_tracker._get_rsi_snapshot("BANKNIFTY", datetime(2026, 7, 23, 9, 15), cache=cache)
        assert mock_fetch.call_count == 2

    def test_fetch_failure_returns_none_and_does_not_cache(self):
        cache: dict = {}
        with (
            patch.object(position_tracker, "datetime", _FrozenDatetime),
            patch("src.state.redis_get", return_value=self._token_map()),
            patch("src.kite_client.fetch_ohlcv", side_effect=RuntimeError("kite down")) as mock_fetch,
        ):
            result1 = position_tracker._get_rsi_snapshot(
                "NIFTY", datetime(2026, 7, 23, 9, 15), cache=cache)
            assert result1 is None
            assert ("NIFTY", _bucket_5m(_FrozenDatetime._now)) not in cache

            mock_fetch.side_effect = None
            mock_fetch.return_value = _fake_df()
            result2 = position_tracker._get_rsi_snapshot(
                "NIFTY", datetime(2026, 7, 23, 9, 15), cache=cache)
            assert result2 is not None
        assert mock_fetch.call_count == 2

    def test_cache_none_skips_caching_entirely(self):
        """No cache dict passed -> every call fetches fresh (used by any
        direct/legacy caller that doesn't opt into caching)."""
        with (
            patch.object(position_tracker, "datetime", _FrozenDatetime),
            patch("src.state.redis_get", return_value=self._token_map()),
            patch("src.kite_client.fetch_ohlcv", return_value=_fake_df()) as mock_fetch,
        ):
            for _ in range(3):
                result = position_tracker._get_rsi_snapshot(
                    "NIFTY", datetime(2026, 7, 23, 9, 15))
                assert result is not None
        assert mock_fetch.call_count == 3

    def test_bucket_5m_floors_at_boundaries(self):
        assert _bucket_5m(datetime(2026, 7, 23, 10, 0, tzinfo=IST)) == \
            datetime(2026, 7, 23, 10, 0, tzinfo=IST).isoformat()
        assert _bucket_5m(datetime(2026, 7, 23, 10, 4, tzinfo=IST)) == \
            datetime(2026, 7, 23, 10, 0, tzinfo=IST).isoformat()
        assert _bucket_5m(datetime(2026, 7, 23, 10, 5, tzinfo=IST)) == \
            datetime(2026, 7, 23, 10, 5, tzinfo=IST).isoformat()
        assert _bucket_5m(datetime(2026, 7, 23, 10, 9, tzinfo=IST)) == \
            datetime(2026, 7, 23, 10, 5, tzinfo=IST).isoformat()
        assert _bucket_5m(datetime(2026, 7, 23, 10, 10, tzinfo=IST)) == \
            datetime(2026, 7, 23, 10, 10, tzinfo=IST).isoformat()


# ──────────────────────────────────────────────────────────────
# Redis write gate + live LTP override — full run_heartbeat() runs
# ──────────────────────────────────────────────────────────────

def _base_pos(tradingsymbol="NIFTY26JUL24600CE", **overrides):
    pos = {
        "tradingsymbol": tradingsymbol,
        "instrument":    "NIFTY",
        "direction":     "CE",
        "entry_price":   100.0,
        "target_t":      None,
        "initial_sl":    50.0,
        "asset_class":   "INDEX",
        "current_ltp":   100.0,
    }
    pos.update(overrides)
    return pos


def _run_heartbeat_with(open_positions_sequence, ltp_map, monkeypatch,
                         is_eod=False, save_mock=None):
    """Runs run_heartbeat() with Kite/paper_engine/trade_notifier mocked out.

    open_positions_sequence: list consumed one item per get_open_positions()
    call (Step 2 call, then Step 4 call — Step 3 is skipped since is_eod=False
    by default).
    ltp_map: {ltp_key: last_price} returned by kite.ltp().
    """
    mock_kite = MagicMock()

    def ltp_side_effect(keys):
        key = keys[0]
        return {key: {"last_price": ltp_map.get(key, 0)}}

    mock_kite.ltp.side_effect = ltp_side_effect

    with (
        patch.object(position_tracker.paper_engine, "get_or_init_daily_capital"),
        patch("src.kite_client.get_kite", return_value=mock_kite),
        patch.object(position_tracker.paper_engine, "entries_blocked", return_value=True),
        patch.object(position_tracker.paper_engine, "get_open_positions",
                     side_effect=open_positions_sequence),
        patch.object(position_tracker.paper_engine, "_exchange_for", return_value="NFO"),
        patch.object(position_tracker.paper_engine, "save_paper_position",
                     save_mock or MagicMock()) as mock_save,
        patch.object(position_tracker.paper_engine, "simulate_exit"),
        patch.object(position_tracker.paper_engine, "is_eod", return_value=is_eod),
        patch.object(position_tracker.paper_engine, "get_closed_positions", return_value=[]),
        patch.object(position_tracker.trade_notifier, "send_paper_consolidated") as mock_notify,
    ):
        status = position_tracker.run_heartbeat()

    return status, mock_save, mock_notify


class TestRedisWriteGate:
    def test_sl_stage_unchanged_save_not_called(self, monkeypatch):
        # progress < 0.5 (ltp close to entry) -> compute_ladder_sl returns
        # prior_sl unchanged, so new_stage == prev_stage.
        pos = _base_pos(target_t=100.0, sl_ladder_stage=50.0)
        ltp_key = "NFO:NIFTY26JUL24600CE"
        _status, mock_save, _notify = _run_heartbeat_with(
            open_positions_sequence=[[pos], [pos]],
            ltp_map={ltp_key: 101.0},  # progress = 0.01 < 0.5
            monkeypatch=monkeypatch,
        )
        mock_save.assert_not_called()

    def test_sl_stage_advanced_save_called_once(self, monkeypatch):
        pos = _base_pos(target_t=100.0, sl_ladder_stage=100.0)
        ltp_key = "NFO:NIFTY26JUL24600CE"
        _status, mock_save, _notify = _run_heartbeat_with(
            open_positions_sequence=[[pos], [pos]],
            ltp_map={ltp_key: 160.0},  # progress = 0.6 -> ladder_sl = 125
            monkeypatch=monkeypatch,
        )
        mock_save.assert_called_once()

    def test_sl_stage_previously_none_save_called(self, monkeypatch):
        pos = _base_pos(target_t=100.0)
        pos.pop("sl_ladder_stage", None)  # never set -> falls back to initial_sl
        ltp_key = "NFO:NIFTY26JUL24600CE"
        _status, mock_save, _notify = _run_heartbeat_with(
            open_positions_sequence=[[pos], [pos]],
            ltp_map={ltp_key: 101.0},  # progress < 0.5, but first write always happens
            monkeypatch=monkeypatch,
        )
        mock_save.assert_called_once()


class TestLiveLtpOverride:
    def test_consolidated_receives_fresh_live_ltp_not_stale(self, monkeypatch):
        ltp_key = "NFO:NIFTY26JUL24600CE"
        step2_pos = _base_pos(current_ltp=10.0)   # stale value pre-fetch
        step4_pos = _base_pos(current_ltp=10.0)   # separate dict simulating a
                                                    # not-yet-caught-up Redis read
        _status, _save, mock_notify = _run_heartbeat_with(
            open_positions_sequence=[[step2_pos], [step4_pos]],
            ltp_map={ltp_key: 55.0},
            monkeypatch=monkeypatch,
        )
        open_now_arg = mock_notify.call_args.args[0]
        assert open_now_arg[0]["current_ltp"] == 55.0

    def test_position_with_no_fetch_this_pass_retains_stored_ltp(self, monkeypatch):
        """LTP fetch fails in Step 2 (continue hit) -> live_ltps never
        populated for this tradingsymbol -> Step 4 must not zero it out."""
        step2_pos = _base_pos(current_ltp=42.0)
        step4_pos = _base_pos(current_ltp=42.0)

        mock_kite = MagicMock()
        mock_kite.ltp.side_effect = RuntimeError("kite ltp down")

        with (
            patch.object(position_tracker.paper_engine, "get_or_init_daily_capital"),
            patch("src.kite_client.get_kite", return_value=mock_kite),
            patch.object(position_tracker.paper_engine, "entries_blocked", return_value=True),
            patch.object(position_tracker.paper_engine, "get_open_positions",
                         side_effect=[[step2_pos], [step4_pos]]),
            patch.object(position_tracker.paper_engine, "_exchange_for", return_value="NFO"),
            patch.object(position_tracker.paper_engine, "save_paper_position"),
            patch.object(position_tracker.paper_engine, "simulate_exit"),
            patch.object(position_tracker.paper_engine, "is_eod", return_value=False),
            patch.object(position_tracker.paper_engine, "get_closed_positions", return_value=[]),
            patch.object(position_tracker.trade_notifier, "send_paper_consolidated") as mock_notify,
        ):
            position_tracker.run_heartbeat()

        open_now_arg = mock_notify.call_args.args[0]
        assert open_now_arg[0]["current_ltp"] == 42.0


# ──────────────────────────────────────────────────────────────
# Regression guard
# ──────────────────────────────────────────────────────────────

class TestReturnValueRegression:
    def test_returns_dict_with_expected_keys_and_ignoring_it_is_safe(self, monkeypatch):
        pos = _base_pos(target_t=None, sl_ladder_stage=50.0)
        ltp_key = "NFO:NIFTY26JUL24600CE"
        status, _save, _notify = _run_heartbeat_with(
            open_positions_sequence=[[pos], [pos]],
            ltp_map={ltp_key: 101.0},
            monkeypatch=monkeypatch,
        )
        assert isinstance(status, dict)
        assert "open_count" in status
        assert "is_eod" in status
        assert status["open_count"] == 1
        assert status["is_eod"] is False

        # Existing callers ignore the return value entirely — must not raise.
        _run_heartbeat_with(
            open_positions_sequence=[[pos], [pos]],
            ltp_map={ltp_key: 101.0},
            monkeypatch=monkeypatch,
        )
