"""Unit tests for the worst-faller EOD square-off gap fix:
- worst_faller_tracker.tracker_tick() unconditional 15:10 EOD square-off
- worst_faller_entry.compute_and_alert() self-healing stale-position guard
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _position(**overrides) -> dict:
    base = {
        "name": "TESTSTK",
        "pe_symbol": "TESTSTK26JULPE100",
        "pe_token": 111,
        "equity_token": 222,
        "strike": 100,
        "expiry": "2026-07-30",
        "lot_size": 500,
        "entry_time": "2026-07-29T15:15:00+05:30",
        "entry_spot": 100.0,
        "entry_opt_price": 5.0,
        "initial_sl_spot": 102.0,
        "current_sl_spot": 101.0,
        "target_pts": 3.0,
        "target_source": "atr",
        "frequency_count": 3,
        "tie_break_used": False,
    }
    base.update(overrides)
    return base


def _quotes():
    return {
        "NSE:222": {"last_price": 98.5, "ohlc": {"high": 99.0, "low": 98.0}},
        "NFO:111": {"last_price": 6.0},
    }


def test_tracker_tick_pre_1510_runs_normal_ladder_logic():
    from src import worst_faller_tracker

    mock_kite = MagicMock()
    mock_kite.quote.return_value = _quotes()

    with (
        patch("src.worst_faller_tracker._load_position", return_value=_position()),
        patch("src.worst_faller_tracker.datetime") as mock_dt,
        patch("src.worst_faller_tracker._rsi_last3", return_value=None),
        patch("src.worst_faller_tracker.compute_ladder_sl", return_value=101.5) as mock_ladder,
        patch("src.worst_faller_tracker.compute_ai_adjusted_sl", return_value=101.5),
        patch("src.worst_faller_tracker.compute_final_sl", return_value=101.5),
        patch("src.worst_faller_tracker.state.redis_set") as mock_set,
        patch("src.worst_faller_tracker.state.redis_delete") as mock_delete,
        patch("src.worst_faller_tracker.worst_faller_notifier.send_close") as mock_close,
        patch("src.worst_faller_tracker.worst_faller_notifier.send_update") as mock_update,
    ):
        mock_dt.now.return_value = datetime(2026, 7, 29, 15, 0, tzinfo=IST)
        worst_faller_tracker.tracker_tick(kite=mock_kite)

    mock_ladder.assert_called_once()
    mock_close.assert_not_called()
    mock_delete.assert_not_called()
    mock_update.assert_called_once()
    mock_set.assert_called_once()


def test_tracker_tick_post_1510_force_closes_eod():
    from src import worst_faller_tracker

    mock_kite = MagicMock()
    mock_kite.quote.return_value = _quotes()

    with (
        patch("src.worst_faller_tracker._load_position", return_value=_position()),
        patch("src.worst_faller_tracker.datetime") as mock_dt,
        patch("src.worst_faller_tracker.compute_ladder_sl") as mock_ladder,
        patch("src.worst_faller_tracker.state.redis_delete") as mock_delete,
        patch("src.worst_faller_tracker.worst_faller_notifier.send_close") as mock_close,
    ):
        mock_dt.now.return_value = datetime(2026, 7, 29, 15, 10, tzinfo=IST)
        worst_faller_tracker.tracker_tick(kite=mock_kite)

    mock_ladder.assert_not_called()
    mock_close.assert_called_once()
    assert mock_close.call_args[0][-1] == "eod_squareoff"
    mock_delete.assert_called_once()


def test_compute_and_alert_force_closes_stale_position_then_enters():
    from src import worst_faller_entry

    mock_kite = MagicMock()
    stale = _position()

    with (
        patch("src.worst_faller_entry.get_kite", return_value=mock_kite),
        patch("src.worst_faller_entry.state.redis_exists", return_value=True),
        patch("src.worst_faller_entry.state.redis_get", return_value=json.dumps(stale)),
        patch("src.worst_faller_entry.state.redis_delete") as mock_delete,
        patch("src.worst_faller_entry.state.redis_set"),
        patch("src.worst_faller_entry.worst_faller_notifier.send_close") as mock_close,
        patch("src.worst_faller_entry.worst_faller_notifier.send_skip") as mock_skip,
        patch("src.worst_faller_entry.worst_faller_universe.pick_worst_faller", return_value=None),
    ):
        mock_kite.quote.return_value = {
            "NSE:222": {"last_price": 98.0},
            "NFO:111": {"last_price": 6.0},
        }
        worst_faller_entry.compute_and_alert(kite=mock_kite)

    mock_close.assert_called_once()
    assert mock_close.call_args[0][-1] == "forced_close_stale_position"
    mock_delete.assert_called_once()
    mock_skip.assert_called_once()


def test_compute_and_alert_no_stale_position_proceeds_straight_to_pick():
    from src import worst_faller_entry

    mock_kite = MagicMock()

    with (
        patch("src.worst_faller_entry.get_kite", return_value=mock_kite),
        patch("src.worst_faller_entry.state.redis_exists", return_value=False),
        patch("src.worst_faller_entry.worst_faller_universe.pick_worst_faller", return_value=None) as mock_pick,
        patch("src.worst_faller_entry.worst_faller_notifier.send_close") as mock_close,
        patch("src.worst_faller_entry.worst_faller_notifier.send_skip") as mock_skip,
    ):
        worst_faller_entry.compute_and_alert(kite=mock_kite)

    mock_close.assert_not_called()
    mock_pick.assert_called_once()
    mock_skip.assert_called_once()
