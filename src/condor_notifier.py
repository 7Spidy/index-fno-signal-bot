"""Discord message builder + poster for the IV Rank iron condor tracker.

Modeled on src/trade_notifier.py's _post_new / _edit_existing helpers, but
with its own persistent (NO daily TTL) message-ID lifecycle: one Discord
message spans the entire life of ONE condor position, potentially across
multiple days, since this system holds overnight.

Uses the SAME webhook env var as the existing trade tracker
(DISCORD_TRADE_TRACKER_WEBHOOK_URL) — posts to the Trade Position Tracker
channel, per the spec.
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

from src import condor_config as ccfg
from src import state

OPEN_COLOR   = 0x4FC3F7   # blue — open / running
PROFIT_COLOR = 0x00C853   # green — closed with positive final P&L
LOSS_COLOR   = 0xF44336   # red — closed with negative final P&L
SKIP_COLOR   = 0x9E9E9E   # gray — skipped entry


def _webhook() -> str | None:
    url = os.environ.get("DISCORD_TRADE_TRACKER_WEBHOOK_URL")
    if not url:
        print("[condor_notifier] DISCORD_TRADE_TRACKER_WEBHOOK_URL not set")
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
        print(f"[condor_notifier] POST returned {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[condor_notifier] POST failed: {e}")
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
            print(f"[condor_notifier] PATCH returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[condor_notifier] PATCH failed: {e}")
        return False


def _one_off_post(embed: dict) -> bool:
    webhook = _webhook()
    if not webhook:
        return False
    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[condor_notifier] one-off POST returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[condor_notifier] one-off POST failed: {e}")
        return False


def _leg_field(leg: dict) -> dict:
    """One embed field for a single leg.

    leg: {"side": "SELL"/"BUY", "tradingsymbol": str, "ltp": float,
          "sl": float | None, "t": float | None}
    LONG (wing) legs pass sl=None, t=None -> rendered as "—".
    """
    sl_str = f"₹{leg['sl']:.2f}" if leg.get("sl") is not None else "—"
    t_str  = f"₹{leg['t']:.2f}" if leg.get("t") is not None else "—"
    return {
        "name": f"{leg['side']} {leg['tradingsymbol']}",
        "value": f"LTP ₹{leg['ltp']:.2f} | SL {sl_str} | T {t_str}",
        "inline": False,
    }


def _subject(iv_rank_entry: float, closed: bool, reason: str | None) -> str:
    title = f"IV Rank Iron Corridor Trade (IV Rank {iv_rank_entry:.1f})"
    if closed:
        title += f" — CLOSED ({reason})"
    return title


def build_embed(
    position: dict,
    legs: list[dict],
    pnl_rs: float,
    closed: bool = False,
    reason: str | None = None,
) -> dict:
    """Build the tracker embed. `position` must contain iv_rank_entry, lots,
    capital_deployed. `legs` is exactly 4 leg dicts (see _leg_field)."""
    iv_rank_entry = position.get("iv_rank_entry", 0.0)
    lots          = position.get("lots", 0)
    capital       = position.get("capital_deployed", 0.0)

    if closed:
        color = PROFIT_COLOR if pnl_rs > 0 else LOSS_COLOR
    else:
        color = OPEN_COLOR

    fields = [_leg_field(leg) for leg in legs]
    fields.append({
        "name": "Capital utilized",
        "value": f"₹{capital:,.0f} ({lots} lot{'s' if lots != 1 else ''})",
        "inline": False,
    })
    sign = "+" if pnl_rs >= 0 else ""
    fields.append({
        "name": "Combined live P&L" if not closed else "Final combined P&L",
        "value": f"{sign}₹{pnl_rs:,.2f}",
        "inline": False,
    })

    return {
        "title":     _subject(iv_rank_entry, closed, reason),
        "color":     color,
        "fields":    fields,
        "footer":    {"text": "Paper simulation only · no real orders · IV Rank Iron Condor (NIFTY)"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_entry(position: dict, legs: list[dict], pnl_rs: float = 0.0) -> bool:
    """First message for a new position. POSTs and stores the msg id
    (no TTL — persists for the life of the position)."""
    embed  = build_embed(position, legs, pnl_rs, closed=False)
    msg_id = _post_new(embed)
    if msg_id:
        state.redis_set(ccfg.REDIS_CONDOR_MSG_ID, msg_id)
        print(f"[condor_notifier] Condor tracker message created (id={msg_id})")
        return True
    return False


def send_update(position: dict, legs: list[dict], pnl_rs: float) -> bool:
    """Tracker-tick update — edits the persistent message in place.

    Falls back to posting a new message if the stored id is missing/stale
    (shouldn't normally happen while REDIS_CONDOR_LOCK is set)."""
    msg_id = state.redis_get(ccfg.REDIS_CONDOR_MSG_ID)
    embed  = build_embed(position, legs, pnl_rs, closed=False)
    if msg_id:
        ok = _edit_existing(msg_id, embed)
        if ok:
            return True
        print("[condor_notifier] edit failed on stored id — posting fresh message")
    new_id = _post_new(embed)
    if new_id:
        state.redis_set(ccfg.REDIS_CONDOR_MSG_ID, new_id)
        return True
    return False


def send_close(position: dict, legs: list[dict], pnl_rs: float, reason: str) -> bool:
    """Final edit on close, then clears the persistent msg-id key so the
    next entry starts a brand-new message."""
    msg_id = state.redis_get(ccfg.REDIS_CONDOR_MSG_ID)
    embed  = build_embed(position, legs, pnl_rs, closed=True, reason=reason)
    ok = False
    if msg_id:
        ok = _edit_existing(msg_id, embed)
    if not ok:
        ok = _one_off_post(embed)
    state.redis_delete(ccfg.REDIS_CONDOR_MSG_ID)
    return ok


def send_skip(iv_rank: float, reason: str) -> bool:
    """One-off gray line when the morning entry is skipped or aborted."""
    embed = {
        "title": (
            f"IV Rank Iron Corridor Trade (IV Rank {iv_rank:.1f}) "
            f"— SKIPPED, gate {ccfg.IV_RANK_THRESHOLD}"
        ),
        "color": SKIP_COLOR,
        "description": reason,
        "footer": {"text": "Paper simulation only · no real orders"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _one_off_post(embed)
