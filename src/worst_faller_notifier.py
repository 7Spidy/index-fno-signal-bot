"""Discord message builder + poster for the 15:15 Worst-Faller PE tracker.

Modeled line-for-line on src/condor_notifier.py's _post_new / _edit_existing /
_one_off_post + persistent msg-id lifecycle: one Discord message spans the
entire life of ONE worst-faller position, potentially across multiple days
(matches the backtest's "let it ride" behaviour — no forced EOD exit).

Uses DISCORD_STOCK_WEBHOOK_URL (the existing stock signal channel, same one
dynamic_stock_universe.py._post_summary() posts to) — NOT the trade-tracker
webhook, per spec decision 5.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src import state

REDIS_MSG_ID_KEY = "worst_faller:msg_id"   # persistent, no TTL — spans the position's life

OPEN_COLOR   = 0x4FC3F7   # blue — open / running
PROFIT_COLOR = 0x00C853   # green — closed with positive final P&L
LOSS_COLOR   = 0xF44336   # red — closed with negative final P&L
SKIP_COLOR   = 0x9E9E9E   # gray — skipped entry


def _webhook() -> str | None:
    url = os.environ.get("DISCORD_STOCK_WEBHOOK_URL")
    if not url:
        print("[worst_faller_notifier] DISCORD_STOCK_WEBHOOK_URL not set")
    return url


def _post_new(embed: dict) -> str | None:
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
        print(f"[worst_faller_notifier] POST returned {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[worst_faller_notifier] POST failed: {e}")
        return None


def _edit_existing(msg_id: str, embed: dict) -> bool:
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
            print(f"[worst_faller_notifier] PATCH returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[worst_faller_notifier] PATCH failed: {e}")
        return False


def _one_off_post(embed: dict) -> bool:
    webhook = _webhook()
    if not webhook:
        return False
    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[worst_faller_notifier] one-off POST returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[worst_faller_notifier] one-off POST failed: {e}")
        return False


def _subject(position: dict, closed: bool, reason: str | None) -> str:
    title = f"15:15 Worst-Faller PE — {position.get('name', '?')}"
    if closed:
        title += f" — CLOSED ({reason})"
    return title


def build_embed(
    position: dict,
    current_spot: float | None,
    current_sl: float | None,
    current_opt_ltp: float | None,
    pnl_rs: float,
    closed: bool = False,
    reason: str | None = None,
) -> dict:
    """Build the tracker embed. `position` is the Redis worst_faller:position
    payload (see worst_faller_entry.py for its exact shape)."""
    if closed:
        color = PROFIT_COLOR if pnl_rs > 0 else LOSS_COLOR
    else:
        color = OPEN_COLOR

    sl_label = "Final SL" if closed else "Current SL"
    sl_str = f"₹{current_sl:.2f}" if current_sl is not None else "—"
    spot_str = f"₹{current_spot:.2f}" if current_spot is not None else "—"
    opt_str = f"₹{current_opt_ltp:.2f}" if current_opt_ltp is not None else "—"

    fields = [
        {
            "name": f"PE {position.get('pe_symbol', '?')}",
            "value": f"Strike {position.get('strike')} | Expiry {position.get('expiry')} | "
                     f"Lot {position.get('lot_size')}",
            "inline": False,
        },
        {
            "name": "Entry",
            "value": f"Spot ₹{position.get('entry_spot', 0):.2f} | "
                     f"Option ₹{position.get('entry_opt_price', 0):.2f}",
            "inline": False,
        },
        {
            "name": "Live" if not closed else "Final",
            "value": f"Spot {spot_str} | Option {opt_str}",
            "inline": False,
        },
        {
            "name": sl_label,
            "value": f"{sl_str} | Target {position.get('target_pts', 0):.1f}pts "
                     f"({position.get('target_source', '?')})",
            "inline": False,
        },
        {
            "name": "Why this stock",
            "value": f"Frequency count {position.get('frequency_count')}/9"
                     + (" (tie-break -> worst W1)" if position.get("tie_break_used") else ""),
            "inline": False,
        },
    ]
    sign = "+" if pnl_rs >= 0 else ""
    fields.append({
        "name": "Live option P&L" if not closed else "Final option P&L",
        "value": f"{sign}₹{pnl_rs:,.2f}",
        "inline": False,
    })

    return {
        "title":     _subject(position, closed, reason),
        "color":     color,
        "fields":    fields,
        "footer":    {"text": "Paper simulation only - no real orders - 15:15 Worst-Faller PE"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_entry(position: dict, pnl_rs: float = 0.0) -> bool:
    """First message for a new position. POSTs and stores the msg id
    (no TTL — persists for the life of the position)."""
    embed = build_embed(
        position, position.get("entry_spot"), position.get("initial_sl_spot"),
        position.get("entry_opt_price"), pnl_rs, closed=False,
    )
    msg_id = _post_new(embed)
    if msg_id:
        state.redis_set(REDIS_MSG_ID_KEY, msg_id)
        print(f"[worst_faller_notifier] tracker message created (id={msg_id})")
        return True
    return False


def send_update(position: dict, current_spot: float, current_sl: float,
                 current_opt_ltp: float, pnl_rs: float) -> bool:
    """Tracker-tick update — edits the persistent message in place. Falls
    back to posting a new message if the stored id is missing/stale."""
    msg_id = state.redis_get(REDIS_MSG_ID_KEY)
    embed = build_embed(position, current_spot, current_sl, current_opt_ltp, pnl_rs, closed=False)
    if msg_id:
        ok = _edit_existing(msg_id, embed)
        if ok:
            return True
        print("[worst_faller_notifier] edit failed on stored id — posting fresh message")
    new_id = _post_new(embed)
    if new_id:
        state.redis_set(REDIS_MSG_ID_KEY, new_id)
        return True
    return False


def send_close(position: dict, final_spot: float, final_sl: float,
                final_opt_ltp: float, pnl_rs: float, reason: str) -> bool:
    """Final edit on close, then clears the persistent msg-id key so the
    next entry starts a brand-new message."""
    msg_id = state.redis_get(REDIS_MSG_ID_KEY)
    embed = build_embed(position, final_spot, final_sl, final_opt_ltp, pnl_rs, closed=True, reason=reason)
    ok = False
    if msg_id:
        ok = _edit_existing(msg_id, embed)
    if not ok:
        ok = _one_off_post(embed)
    state.redis_delete(REDIS_MSG_ID_KEY)
    return ok


def send_skip(reason: str) -> bool:
    """One-off gray line when today's entry is skipped or aborted."""
    embed = {
        "title": "15:15 Worst-Faller PE — SKIPPED",
        "color": SKIP_COLOR,
        "description": reason,
        "footer": {"text": "Paper simulation only - no real orders - 15:15 Worst-Faller PE"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _one_off_post(embed)
