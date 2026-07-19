"""Unit tests for condor_notifier.py — persistent (non-daily-TTL) message
lifecycle, embed content, and subject-line formatting. All Discord/Redis
calls are mocked — no live HTTP calls.
"""
import os
import unittest.mock as mock

os.environ["DISCORD_TRADE_TRACKER_WEBHOOK_URL"] = "https://example.com/fake-webhook"

from src import condor_notifier  # noqa: E402
from src import condor_config as ccfg  # noqa: E402


def _mock_post_response():
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": "999888777"}
    return resp


def _mock_patch_response():
    resp = mock.MagicMock()
    resp.status_code = 200
    return resp


def _position(iv_rank=42.5, lots=2, capital=95000.0):
    return {"iv_rank_entry": iv_rank, "lots": lots, "capital_deployed": capital}


def _legs():
    return [
        {"side": "SELL", "tradingsymbol": "NIFTY26AUG25000CE", "ltp": 55.0, "sl": 55.0, "t": 33.0},
        {"side": "BUY", "tradingsymbol": "NIFTY26AUG25100CE", "ltp": 25.0, "sl": None, "t": None},
        {"side": "SELL", "tradingsymbol": "NIFTY26AUG24800PE", "ltp": 48.0, "sl": 48.0, "t": 28.8},
        {"side": "BUY", "tradingsymbol": "NIFTY26AUG24700PE", "ltp": 20.0, "sl": None, "t": None},
    ]


class TestPersistentMessageLifecycle:
    def test_first_call_posts_and_stores_id(self):
        with mock.patch("src.condor_notifier.requests.post", return_value=_mock_post_response()), \
             mock.patch("src.condor_notifier.state.redis_set") as mock_set:
            ok = condor_notifier.send_entry(_position(), _legs(), pnl_rs=0.0)
        assert ok is True
        mock_set.assert_called_once_with(ccfg.REDIS_CONDOR_MSG_ID, "999888777")

    def test_subsequent_call_patches_same_id(self):
        with mock.patch("src.condor_notifier.requests.patch", return_value=_mock_patch_response()) as mock_patch, \
             mock.patch("src.condor_notifier.state.redis_get", return_value="999888777"):
            ok = condor_notifier.send_update(_position(), _legs(), pnl_rs=1250.0)
        assert ok is True
        mock_patch.assert_called_with(
            "https://example.com/fake-webhook/messages/999888777",
            json=mock.ANY,
            timeout=10,
        )

    def test_close_edits_then_deletes_msg_id(self):
        with mock.patch("src.condor_notifier.requests.patch", return_value=_mock_patch_response()), \
             mock.patch("src.condor_notifier.state.redis_get", return_value="999888777"), \
             mock.patch("src.condor_notifier.state.redis_delete") as mock_del:
            ok = condor_notifier.send_close(_position(), _legs(), pnl_rs=2000.0, reason="target")
        assert ok is True
        mock_del.assert_called_once_with(ccfg.REDIS_CONDOR_MSG_ID)


class TestEmbedContent:
    def test_capital_utilized_present(self):
        embed = condor_notifier.build_embed(_position(capital=95000.0), _legs(), pnl_rs=100.0)
        values = " ".join(f["value"] for f in embed["fields"])
        assert "95,000" in values or "95000" in values

    def test_four_leg_fields_present(self):
        embed = condor_notifier.build_embed(_position(), _legs(), pnl_rs=100.0)
        leg_names = [f["name"] for f in embed["fields"] if "SELL" in f["name"] or "BUY" in f["name"]]
        assert len(leg_names) == 4

    def test_long_legs_show_sl_and_t_dash(self):
        embed = condor_notifier.build_embed(_position(), _legs(), pnl_rs=100.0)
        long_fields = [f for f in embed["fields"] if f["name"].startswith("BUY")]
        assert len(long_fields) == 2
        for f in long_fields:
            assert "SL —" in f["value"]
            assert "T —" in f["value"]

    def test_subject_line_verbatim(self):
        embed = condor_notifier.build_embed(_position(iv_rank=42.5), _legs(), pnl_rs=0.0)
        assert embed["title"] == "IV Rank Iron Corridor Trade (IV Rank 42.5)"

    def test_subject_line_closed_appends_reason(self):
        embed = condor_notifier.build_embed(
            _position(iv_rank=42.5), _legs(), pnl_rs=-500.0, closed=True, reason="trailing_sl",
        )
        assert embed["title"] == "IV Rank Iron Corridor Trade (IV Rank 42.5) — CLOSED (trailing_sl)"
