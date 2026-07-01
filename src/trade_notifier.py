"""Discord embed builders for trade position tracking — FYI, Action, and Exit."""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")

FYI_COLOR    = 0x4FC3F7
ACTION_COLOR = 0xFF9F43
EXIT_COLOR   = 0x4FC3F7


def _webhook() -> str | None:
    url = os.environ.get("DISCORD_TRADE_TRACKER_WEBHOOK_URL")
    if not url:
        print("[trade_notifier] DISCORD_TRADE_TRACKER_WEBHOOK_URL not set")
    return url


def _post(embed: dict) -> bool:
    webhook = _webhook()
    if not webhook:
        return False
    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[trade_notifier] Discord returned {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[trade_notifier] Discord POST failed: {e}")
        return False


def send_fyi(
    instrument: str,
    direction: str,
    ltp: float,
    progress_pct: float,
    current_sl: float,
) -> bool:
    """Post a blue FYI update — SL is already at or ahead of required level."""
    arrow = "↑" if direction.upper() == "CE" else "↓"
    embed = {
        "title":  f"ℹ️ {instrument} {direction.upper()} — Position Update",
        "color":  FYI_COLOR,
        "fields": [
            {"name": "LTP",         "value": f"₹{ltp:,.2f}",        "inline": True},
            {"name": "Progress",    "value": f"{progress_pct:.1f}%",  "inline": True},
            {"name": "Current SL",  "value": f"₹{current_sl:,.2f}",  "inline": True},
            {"name": "Direction",   "value": f"{direction.upper()} {arrow}", "inline": True},
        ],
        "footer":    {"text": "SL up to date · no action needed"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _post(embed)


def send_action(
    instrument: str,
    direction: str,
    ltp: float,
    progress_pct: float,
    current_sl_kite: float,
    required_sl: float,
) -> bool:
    """Post an amber ACTION alert — manual SL update needed in Kite."""
    arrow = "↑" if direction.upper() == "CE" else "↓"
    embed = {
        "title":       f"⚠️ ACTION: Move SL — {instrument} {direction.upper()}",
        "color":       ACTION_COLOR,
        "description": "Your Kite SL is behind the trailing ladder. Move it manually.",
        "fields": [
            {"name": "LTP",           "value": f"₹{ltp:,.2f}",             "inline": True},
            {"name": "Progress",      "value": f"{progress_pct:.1f}%",       "inline": True},
            {"name": "Direction",     "value": f"{direction.upper()} {arrow}", "inline": True},
            {"name": "Current Kite SL", "value": f"₹{current_sl_kite:,.2f}", "inline": True},
            {"name": "Required SL",   "value": f"**₹{required_sl:,.2f}**",  "inline": True},
        ],
        "footer":    {"text": "Alert only · move SL in Kite manually"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _post(embed)


def send_position_detected(
    instrument: str,
    direction: str,
    tradingsymbol: str,
    entry_price: float,
    sl: float | None,
    target_t: float | None,
    qty: int,
) -> bool:
    """Post a blue alert once a position is confirmed across 2 heartbeats."""
    arrow = "↑" if direction.upper() == "CE" else "↓"
    sl_str = f"₹{sl:,.2f}" if sl is not None else "unavailable"
    t_str  = f"{target_t:,.2f} pts" if target_t is not None else "unavailable"
    embed = {
        "title":  f"🎯 {instrument} {direction.upper()} — Position Detected",
        "color":  FYI_COLOR,
        "fields": [
            {"name": "Tradingsymbol", "value": tradingsymbol,             "inline": True},
            {"name": "Entry (avg)",   "value": f"₹{entry_price:,.2f}",    "inline": True},
            {"name": "Qty",           "value": str(qty),                  "inline": True},
            {"name": "Direction",     "value": f"{direction.upper()} {arrow}", "inline": True},
            {"name": "Initial SL",    "value": sl_str,                    "inline": True},
            {"name": "Target (T)",    "value": t_str,                     "inline": True},
        ],
        "footer":    {"text": "Auto-detected via Kite positions · confirmed across 2 heartbeats"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _post(embed)


def send_partial_exit(
    instrument: str,
    direction: str,
    tradingsymbol: str,
    old_qty: int,
    new_qty: int,
) -> bool:
    """Post an amber note when a tracked position's quantity decreases but stays open."""
    embed = {
        "title":       f"✂️ Partial Exit — {instrument} {direction.upper()}",
        "color":       ACTION_COLOR,
        "description": f"Partial exit detected on {tradingsymbol}: {old_qty} → {new_qty}.",
        "footer":      {"text": "SL / target unchanged · ladder is price-driven, not qty-driven"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    return _post(embed)


def send_exit_summary(
    instrument: str,
    direction: str,
    entry: float,
    exit_price: float,
    pnl: float,
    r_multiple: float | None,
    compliance_ratio: float,
    market_note: str,
    exit_type: str | None = None,
) -> bool:
    """Post a blue Exit summary with trade stats.

    exit_type labels how the exit was detected, e.g. "Ladder SL" vs
    "Manual / untracked flatten" — always posted regardless of which.
    """
    pnl_sign = "+" if pnl >= 0 else ""
    if r_multiple is not None:
        r_sign  = "+" if r_multiple >= 0 else ""
        r_field = f"{r_sign}{r_multiple:.2f}R"
    else:
        r_field = "—"
    embed = {
        "title":  f"🏁 {instrument} {direction.upper()} — Position Closed",
        "color":  EXIT_COLOR,
        "fields": [
            {"name": "Entry",      "value": f"₹{entry:,.2f}",          "inline": True},
            {"name": "Exit",       "value": f"₹{exit_price:,.2f}",      "inline": True},
            {"name": "P&L",        "value": f"{pnl_sign}₹{pnl:,.2f}",  "inline": True},
            {"name": "R-Multiple", "value": r_field,                    "inline": True},
            {
                "name":   "SL Compliance",
                "value":  f"{compliance_ratio:.0%} of action alerts acknowledged",
                "inline": True,
            },
            {"name": "Exit Type",   "value": exit_type or "—", "inline": True},
            {"name": "Market note", "value": market_note or "—", "inline": False},
        ],
        "footer":    {"text": "Alert only · trade complete"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return _post(embed)
