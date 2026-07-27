"""Discord message builders for paper-trade position tracking."""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src import state

IST = ZoneInfo("Asia/Kolkata")

OPEN_COLOR   = 0x4FC3F7   # blue — open positions
CLOSED_COLOR = 0x00C853   # green — profitable day
LOSS_COLOR   = 0xF44336   # red — loss day
SKIP_COLOR   = 0x9E9E9E   # gray — skipped entry


def _webhook() -> str | None:
    url = os.environ.get("DISCORD_TRADE_TRACKER_WEBHOOK_URL")
    if not url:
        print("[trade_notifier] DISCORD_TRADE_TRACKER_WEBHOOK_URL not set")
    return url


def _post_new(embed: dict) -> str | None:
    """POST a new message and return the message ID (requires ?wait=true)."""
    webhook = _webhook()
    if not webhook:
        return None
    try:
        resp = requests.post(
            webhook + "?wait=true",
            json={"embeds": [embed]},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            data = resp.json()
            return str(data.get("id", ""))
        print(f"[trade_notifier] POST returned {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[trade_notifier] POST failed: {e}")
        return None


def _edit_existing(msg_id: str, embed: dict) -> bool:
    """PATCH an existing message to update it in place."""
    webhook = _webhook()
    if not webhook:
        return False
    try:
        resp = requests.patch(
            f"{webhook}/messages/{msg_id}",
            json={"embeds": [embed]},
            timeout=10,
        )
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[trade_notifier] PATCH returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[trade_notifier] PATCH failed: {e}")
        return False


def _msg_id_key(date_str: str) -> str:
    return f"paper:discord_msg_id:{date_str}"


def _ladder_progress_stages(entry, T, ltp) -> tuple[float | None, float] | None:
    """Current (last-crossed) and next progress-ladder thresholds, as T-fractions.

    Progress is always (ltp - entry) / T regardless of CE/PE -- option
    premiums rise for both when a trade is winning (see the PE
    sign-inversion note in _build_consolidated_embed), and this must mirror
    position_tracker.compute_ladder_sl, which always uses its "CE" branch
    for both directions. If that ladder ever changes, update this too.
    """
    if not T or T <= 0:
        return None
    progress = (ltp - entry) / T
    thresholds = [0.5, 0.9, 1.0]
    if progress > 1.0:
        n = math.floor(round((progress - 1.0) / 0.1, 9)) + 2
        thresholds = thresholds + [1.0 + 0.1 * i for i in range(1, n)]

    crossed = [t for t in thresholds if progress >= t]
    current_stage = crossed[-1] if crossed else None
    remaining = [t for t in thresholds if t not in crossed]
    next_stage = remaining[0] if remaining else thresholds[-1] + 0.1
    return current_stage, next_stage


def _result_and_trail_label(rec) -> str:
    """PROFIT/LOSS (by net P&L) plus whether the trailing SL ever advanced
    past its initial value before this exit."""
    pnl = rec.get("pnl_net", 0)
    result_label = "PROFIT" if pnl >= 0 else "LOSS"
    initial_sl = rec.get("initial_sl")
    trailed = initial_sl is not None and rec.get("exit_sl_stage") != initial_sl
    if trailed:
        trail_note = "SL trailed"
    elif initial_sl is not None:
        trail_note = "exited at initial SL"
    else:
        trail_note = ""
    return f"{result_label} · {trail_note}" if trail_note else result_label


def _build_consolidated_embed(
    open_positions: list[dict],
    closed_positions: list[dict],
    date_str: str,
) -> dict:
    fields = []

    if open_positions:
        for pos in open_positions:
            arrow = "↑" if pos.get("direction") == "CE" else "↓"
            ltp   = pos.get("current_ltp", pos.get("entry_price", 0))
            entry = pos.get("entry_price", 0)
            sl    = pos.get("sl_ladder_stage", 0)
            ls    = pos.get("lot_size", 1)
            direction = pos.get("direction", "?")
            T     = pos.get("target_t")

            # Unrealized gross P&L (before charges — shown as estimate)
            # Options are always bought long (CE or PE) — premium rising is
            # always profit, regardless of option type. Do not branch on
            # direction here (that was the PE sign-inversion bug).
            unreal = (ltp - entry) * ls
            sign = "+" if unreal >= 0 else ""

            stages = _ladder_progress_stages(entry, T, ltp)
            value_lines = [f"Entry ₹{entry:.2f} · LTP ₹{ltp:.2f} · SL ₹{sl:.2f}"]
            if stages:
                current_stage, next_stage = stages
                parts = []
                if current_stage is not None:
                    parts.append(f"{current_stage:.1f}T = ₹{entry + current_stage * T:.2f} (current)")
                parts.append(f"{next_stage:.1f}T = ₹{entry + next_stage * T:.2f} (next)")
                value_lines.append(" · ".join(parts))
            value_lines.append(f"Unrealized ≈ {sign}₹{unreal:.0f} (gross, est.)")

            fields.append({
                "name": f"{pos.get('tradingsymbol', pos['instrument'])} {direction} {arrow} [OPEN]",
                "value": "\n".join(value_lines),
                "inline": False,
            })

    if closed_positions:
        for rec in closed_positions:
            arrow = "↑" if rec.get("direction") == "CE" else "↓"
            pnl   = rec.get("pnl_net", 0)
            sign  = "+" if pnl >= 0 else ""
            result_label = _result_and_trail_label(rec)
            fields.append({
                "name": f"{rec.get('tradingsymbol', rec['instrument'])} {rec['direction']} {arrow} [CLOSED]",
                "value": (
                    f"Entry ₹{rec['entry_price']:.2f} · Exit ₹{rec['exit_price']:.2f} · "
                    f"Net {sign}₹{pnl:.2f}\n"
                    f"{result_label}"
                ),
                "inline": False,
            })

    if not fields:
        fields.append({
            "name": "No activity",
            "value": "No open or closed paper trades yet today.",
            "inline": False,
        })

    return {
        "title":     f"📊 Paper Trade — {date_str}",
        "color":     OPEN_COLOR,
        "fields":    fields,
        "footer":    {"text": "Paper simulation only · no real orders · updated each cycle"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_paper_consolidated(
    open_positions: list[dict],
    closed_positions: list[dict],
    date_str: str,
) -> bool:
    """Post or edit the single consolidated paper-trade message for the day.

    On the first call of the day: POST with ?wait=true, store the returned
    message ID in Redis (paper:discord_msg_id:{date}).
    On every subsequent call: PATCH the same message ID to update it in place.
    """
    embed  = _build_consolidated_embed(open_positions, closed_positions, date_str)
    id_key = _msg_id_key(date_str)

    existing_id = state.redis_get(id_key)
    if existing_id:
        return _edit_existing(existing_id, embed)

    # First call today — create new message and save ID
    msg_id = _post_new(embed)
    if msg_id:
        state.redis_set(id_key, msg_id, ex=86400)
        print(f"[trade_notifier] Paper consolidated message created (id={msg_id})")
        return True
    return False


def send_paper_eod_summary(
    closed_positions: list[dict],
    total_pnl: float,
    date_str: str,
) -> bool:
    """Post a distinct EOD summary message (called exactly once per day)."""
    wins   = sum(1 for r in closed_positions if r.get("pnl_net", 0) > 0)
    losses = sum(1 for r in closed_positions if r.get("pnl_net", 0) <= 0)
    sign   = "+" if total_pnl >= 0 else ""
    color  = CLOSED_COLOR if total_pnl >= 0 else LOSS_COLOR

    lines = []
    for rec in closed_positions:
        p    = rec.get("pnl_net", 0)
        psign = "+" if p >= 0 else ""
        arrow = "↑" if rec.get("direction") == "CE" else "↓"
        lines.append(
            f"{rec.get('tradingsymbol', rec['instrument'])} {rec['direction']} {arrow} "
            f"entry={rec['entry_price']:.2f} exit={rec['exit_price']:.2f} "
            f"net={psign}₹{p:.2f} ({rec.get('reason', '')})"
        )

    breakdown = "\n".join(lines) if lines else "No trades executed today."
    embed = {
        "title":       f"🏁 Paper EOD Summary — {date_str}",
        "color":       color,
        "description": f"**Total realized net P&L: {sign}₹{total_pnl:.2f}**",
        "fields": [
            {"name": "Wins",   "value": str(wins),   "inline": True},
            {"name": "Losses", "value": str(losses),  "inline": True},
            {"name": "Trades", "value": str(wins + losses), "inline": True},
            {"name": "Per-trade breakdown", "value": f"```\n{breakdown}\n```", "inline": False},
        ],
        "footer":    {"text": "Paper simulation · charges are approximate (see src/charges.py)"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    webhook = _webhook()
    if not webhook:
        return False
    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[trade_notifier] EOD POST returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[trade_notifier] EOD POST failed: {e}")
        return False


def send_trade_skipped(
    instrument: str,
    tradingsymbol: str,
    direction: str,
    reason: str,
) -> bool:
    """Post a one-off notice when a signal is skipped instead of entered."""
    arrow = "↑" if direction.upper() == "CE" else "↓"
    embed = {
        "title":       f"⏭️ Skipped — {tradingsymbol} {direction.upper()} {arrow}",
        "color":       SKIP_COLOR,
        "description": reason,
        "footer":      {"text": "Paper simulation only · no real orders"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }

    webhook = _webhook()
    if not webhook:
        return False
    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[trade_notifier] Skip POST returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[trade_notifier] Skip POST failed: {e}")
        return False
