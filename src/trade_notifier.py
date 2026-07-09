"""Discord message builders for paper-trade position tracking."""
from __future__ import annotations

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
EOD_COLOR    = 0x9C27B0   # purple — end-of-day summary


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

            # Unrealized gross P&L (before charges — shown as estimate)
            if direction == "CE":
                unreal = (ltp - entry) * ls
            else:
                unreal = (entry - ltp) * ls
            sign = "+" if unreal >= 0 else ""

            fields.append({
                "name": f"{pos['instrument']} {direction} {arrow} [OPEN]",
                "value": (
                    f"Entry ₹{entry:.2f} · LTP ₹{ltp:.2f} · SL ₹{sl:.2f}\n"
                    f"Unrealized ≈ {sign}₹{unreal:.0f} (gross, est.)"
                ),
                "inline": False,
            })

    if closed_positions:
        for rec in closed_positions:
            arrow = "↑" if rec.get("direction") == "CE" else "↓"
            pnl   = rec.get("pnl_net", 0)
            sign  = "+" if pnl >= 0 else ""
            fields.append({
                "name": f"{rec['instrument']} {rec['direction']} {arrow} [CLOSED]",
                "value": (
                    f"Entry ₹{rec['entry_price']:.2f} · Exit ₹{rec['exit_price']:.2f} · "
                    f"Net {sign}₹{pnl:.2f} · {rec.get('reason', '')}"
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
            f"{rec['instrument']} {rec['direction']} {arrow} "
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
