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
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("[notifier] DISCORD_WEBHOOK_URL not set")
        return False

    is_ce  = direction.upper() == "CE"
    color  = CE_COLOR if is_ce else PE_COLOR
    emoji  = "🟢" if is_ce else "🔴"
    arrow  = "↑" if is_ce else "↓"

    atm     = result.get("atm_data", {})
    ts      = atm.get("tradingsymbol", "unavailable")
    strike  = atm.get("strike")
    expiry  = atm.get("expiry", "—")
    ftime   = atm.get("fetch_time", "—")

    atm_ltp    = result.get("atm_ltp")
    opt_target = result.get("opt_target")
    opt_sl     = result.get("opt_sl")
    spot_ltp   = result.get("spot_ltp")
    spot_tgt   = result.get("spot_tgt")
    spot_sl    = result.get("spot_sl")
    spread     = result.get("fut_spot_spread")

    def fp(v):
        return f"₹{v:,.2f}" if v is not None else "unavailable"

    def fi(v):
        return f"{v:,.1f}" if v is not None else "—"

    buy_sub    = f"live LTP @ {ftime}"
    tgt_sub    = f"if {instrument} spot → {fi(spot_tgt)}"
    sl_sub     = f"if {instrument} spot → {fi(spot_sl)}"

    asset_class = result.get("asset_class", "INDEX")
    if asset_class == "STOCK":
        px_field = {"name": "Spot",
                    "value": fi(result.get("futures_price")),
                    "inline": True}
    else:
        fut_str = fi(result.get("futures_price"))
        if spread and abs(spread) > 5:
            fut_str += f"  (spot +{abs(spread):.0f} pts)"
        px_field = {"name": "Futures / Spot", "value": fut_str, "inline": True}

    vwap_val  = result.get("vwap")
    spot_ref  = spot_ltp or 0
    vwap_dir  = "↑ above" if spot_ref > (vwap_val or 0) else "↓ below"

    expiry_note = " (rolled forward)" if atm.get("rolled_forward") else ""

    fields = [
        {
            "name":   "Buy this option",
            "value":  ts,
            "inline": False,
        },
        {
            "name":   "Contract",
            "value":  f"{strike} {direction.upper()}  ·  {expiry} expiry{expiry_note}",
            "inline": False,
        },
        {
            "name":   "Buy at",
            "value":  f"`{fp(atm_ltp)}`\n{buy_sub}",
            "inline": True,
        },
        {
            "name":   "Target",
            "value":  f"**{fp(opt_target)}**\n{tgt_sub}",
            "inline": True,
        },
        {
            "name":   "Stop Loss",
            "value":  f"**{fp(opt_sl)}**\n{sl_sub}",
            "inline": True,
        },
        {
            "name":   "Conviction",
            "value":  (f"{result.get('conviction', '—')}"
                       f"  ·  1:{result.get('rr', '—')}"),
            "inline": True,
        },
        px_field,
        {
            "name":   "Candle",
            "value":  result.get("candle_time", "—"),
            "inline": True,
        },
        {
            "name":   "RSI(14)",
            "value":  f"{result.get('rsi', 0):.1f} {arrow}",
            "inline": True,
        },
        {
            "name":   "+DI / −DI",
            "value":  (f"{result.get('pdi', 0):.1f} / "
                       f"{result.get('ndi', 0):.1f}"),
            "inline": True,
        },
        {
            "name":   "VWAP",
            "value":  (fi(vwap_val) + "  " + vwap_dir),
            "inline": True,
        },
        {
            "name":   "Conditions",
            "value":  (
                f"{'✅' if result.get('c1') else '❌'} "
                f"Candle {arrow} prev close\n"
                f"{'✅' if result.get('c2') else '❌'} "
                f"VWAP cross-{'up' if is_ce else 'dn'} ≤30min\n"
                f"{'✅' if result.get('c3') else '❌'} "
                f"RSI {'rising' if is_ce else 'falling'} (3 candles)\n"
                f"{'✅' if result.get('c4') else '❌'} "
                f"{'+' if is_ce else '−'}DI > 25, dominant & rising"
            ),
            "inline": False,
        },
    ]

    embed = {
        "title":     f"{emoji} {direction.upper()} Signal — {instrument}",
        "color":     color,
        "fields":    fields,
        "footer":    {
            "text": (
                "Alert only  ·  Buy/Target/SL are option premium levels  ·  "
                "verify before trading"
            )
        },
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[notifier] Discord returned {resp.status_code}: {resp.text[:200]}")
        else:
            print(f"[notifier] ✓ Discord signal sent: {instrument} {direction}")
        return ok
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


