"""Boundary + wiring tests for the centralised square-off anchor
(config.SQUAREOFF_IST). Guards against the pre-2026-08-02 failure mode where
paper_engine and tools/pnl_replay each carried their own hardcoded 15:10.
"""
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from src import config, paper_engine

IST = ZoneInfo("Asia/Kolkata")


def _at(h, m):
    return datetime(2026, 7, 29, h, m, tzinfo=IST)


class TestAnchorWiring:
    def test_config_exposes_squareoff(self):
        assert config.SQUAREOFF_IST == "15:00"
        assert config.SQUAREOFF_HHMM == (15, 0)

    def test_paper_engine_derives_from_config(self):
        assert (paper_engine._EOD_HOUR, paper_engine._EOD_MINUTE) == config.SQUAREOFF_HHMM

    def test_pnl_replay_derives_from_config(self):
        from tools.pnl_replay import SQUAREOFF_TIME
        assert SQUAREOFF_TIME == dtime(*config.SQUAREOFF_HHMM)

    def test_eval_window_end_moved(self):
        assert config.EVAL_WINDOW_END == "14:30"
        assert config.EVAL_WINDOW_IST == ("09:40", "14:30")

    def test_entry_cutoff_precedes_squareoff(self):
        """Structural invariant: no-new-entry must be strictly before
        square-off, with runway of at least the 15-min theta time-stop."""
        eh, em = map(int, config.EVAL_WINDOW_END.split(":"))
        sh, sm = config.SQUAREOFF_HHMM
        runway_min = (sh * 60 + sm) - (eh * 60 + em)
        assert runway_min >= 15


class TestIsEodBoundary:
    def test_1459_is_not_eod(self):
        assert paper_engine.is_eod(_at(14, 59)) is False

    def test_1500_is_eod(self):
        assert paper_engine.is_eod(_at(15, 0)) is True

    def test_1501_is_eod(self):
        assert paper_engine.is_eod(_at(15, 1)) is True

    def test_1600_is_eod(self):
        """Hour-major tuple compare must stay correct past the hour rollover."""
        assert paper_engine.is_eod(_at(16, 0)) is True

    def test_0940_is_not_eod(self):
        assert paper_engine.is_eod(_at(9, 40)) is False
