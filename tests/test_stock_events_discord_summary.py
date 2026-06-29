"""
Tests for stock_events._post_discord_summary() and universe size invariants.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from src import stock_config as cfg
from src.stock_events import _post_discord_summary, _NAMES, MARKETAUX_SYMBOL_MAP


# ---------------------------------------------------------------------------
# Universe size invariant
# ---------------------------------------------------------------------------

def test_names_length_matches_cfg_stocks():
    """Catches future drift between cfg.STOCKS and the Marketaux symbol map."""
    assert len(_NAMES) == len(cfg.STOCKS) == 14


# ---------------------------------------------------------------------------
# _post_discord_summary — no exclusions, all batches succeeded
# ---------------------------------------------------------------------------

def test_all_stocks_active_message_uses_total_stocks_param():
    """The 'all active' description must reflect total_stocks, not a hardcoded literal."""
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    with patch.dict("os.environ", {"DISCORD_STOCK_WEBHOOK_URL": "https://example.com/hook"}):
        with patch("src.stock_events.requests.post", side_effect=fake_post):
            _post_discord_summary(
                excluded=[],
                failures=0,
                total_batches=4,
                total_stocks=14,
            )

    description = captured["payload"]["embeds"][0]["description"]
    assert "All 14 tracked stocks active today" in description


def test_total_stocks_param_is_not_hardcoded():
    """Passing total_stocks=11 must produce '11', proving the value is parameterised."""
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        return MagicMock()

    with patch.dict("os.environ", {"DISCORD_STOCK_WEBHOOK_URL": "https://example.com/hook"}):
        with patch("src.stock_events.requests.post", side_effect=fake_post):
            _post_discord_summary(
                excluded=[],
                failures=0,
                total_batches=3,
                total_stocks=11,
            )

    description = captured["payload"]["embeds"][0]["description"]
    assert "All 11 tracked stocks active today" in description
    assert "14" not in description
