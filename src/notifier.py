"""Discord webhook notifier — fires only on confirmed signals."""
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

CE_COLOR = 0x00E5A0
PE_COLOR = 0xF87171
WARN_COLOR = 0xF59E0B


def send_signal(instrument: str, direction: str, result: dict) -> bool:
    """Post a rich Discord embed for a CE or PE signal. Returns True on success."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[notifier] No DISCORD_WEBHOOK_URL — skipping")
        return False

    is_ce = direction.upper() == "CE"
    color = CE_COLOR if is_ce else PE_COLOR
    emoji = "🟢" if is_ce else "🔴"
    di_label = "+DI" if is_ce else "-DI"
    di_val = result.get("pdi") if is_ce else result.get("mdi")
    opp_di_label = "-DI" if is_ce else "+DI"
    opp_di_val = result.get("mdi") if is_ce else result.get("pdi")

    price = result.get("futures_price") or 0.0
    atm = result.get("atm_strike") or 0
    strike_step = result.get("strike_step", 50)
    vwap_val = result.get("vwap")
    rsi_val = result.get("rsi")
    candle_time = result.get("candle_time", "")

    vwap_delta_str = ""
    if vwap_val:
        delta = price - vwap_val
        sign = "+" if delta >= 0 else ""
        vwap_delta_str = f"{vwap_val:,.1f} ({sign}{delta:,.0f} pts)"

    conds = result.get("ce" if is_ce else "pe", {})
    cond_labels = {
        "c1": f"Candle closes {'above' if is_ce else 'below'} prior",
        "c2": f"VWAP cross-{'up' if is_ce else 'down'} ≤30min",
        "c3": f"RSI {'rising' if is_ce else 'falling'} (3 candles)",
        "c4": f"{di_label} > 25 & dominant",
    }
    cond_str = "\n".join(
        f"{'✅' if conds.get(k) else '❌'} {label}"
        for k, label in cond_labels.items()
    )

    try:
        candle_ist = _format_candle_time(candle_time)
    except Exception:
        candle_ist = candle_time

    embed = {
        "title": f"{emoji} {direction.upper()} Signal — {instrument}",
        "color": color,
        "fields": [
            {
                "name": "Futures Price | ATM Strike",
                "value": f"`{price:,.2f}` | `{atm:,} {direction.upper()}`",
                "inline": False,
            },
            {
                "name": "Candle (IST) | RSI(14)",
                "value": f"`{candle_ist}` | `{rsi_val:.1f}`" if rsi_val else f"`{candle_ist}` | n/a",
                "inline": False,
            },
            {
                "name": f"{di_label} / {opp_di_label}",
                "value": f"`{di_val:.1f} / {opp_di_val:.1f}`" if di_val and opp_di_val else "n/a",
                "inline": False,
            },
            {
                "name": "VWAP | vs Price",
                "value": f"`{vwap_delta_str}`" if vwap_delta_str else "n/a",
                "inline": False,
            },
            {
                "name": "Conditions",
                "value": cond_str,
                "inline": False,
            },
        ],
        "footer": {"text": "Alert only · verify before trading"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        r.raise_for_status()
        print(f"[notifier] ✓ Discord signal sent: {instrument} {direction}")
        return True
    except Exception as e:
        print(f"[notifier] Discord POST failed: {e}")
        return False


def send_warning(message: str) -> None:
    """Post a plain warning embed to Discord."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return
    embed = {
        "title": "⚠️ Signal Bot Warning",
        "description": message,
        "color": WARN_COLOR,
        "footer": {"text": "index-fno-signal-bot"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"[notifier] Warning POST failed: {e}")


def _format_candle_time(ts_str: str) -> str:
    """Convert ISO timestamp string to HH:MM IST."""
    from dateutil import parser as dtparser
    dt = dtparser.parse(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime("%H:%M IST")
