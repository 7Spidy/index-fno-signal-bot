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

CE_COLOR         = 0x00E5A0
PE_COLOR         = 0xF87171
WARN_COLOR       = 0xF59E0B
SUPPRESSED_COLOR = 0x6B7280   # gray — visually distinct from CE/PE/warning colors


def _supertrend_field_value(direction: str, aligned: bool | None) -> str:
    """result's c5 is already the direction-specific "aligned" boolean
    (ce.c5 for a CE signal, pe.c5 for a PE signal) — True means Supertrend
    matches this trade's direction (CE=up, PE=down). Recover the raw trend
    label from (direction, aligned) rather than reading c5 as if it were the
    raw uptrend/downtrend flag, since that flips meaning between CE and PE."""
    is_ce = direction.upper() == "CE"
    raw_uptrend = aligned if is_ce else not aligned
    label = "Uptrend" if raw_uptrend else "Downtrend"
    return label if aligned else f"⚠️ {label} ⚠️"


def _build_trade_fields(instrument: str, direction: str, result: dict) -> list[dict]:
    """Build the shared trade-detail field list used by both send_signal()
    and send_suppressed_signal() (instrument, buy/target/SL, conditions, etc)."""
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

    def fp(v):
        return f"₹{v:,.2f}" if v is not None else "unavailable"

    def fi(v):
        return f"{v:,.1f}" if v is not None else "—"

    delta_used     = result.get("delta_used", 0.50)
    delta_fallback = result.get("delta_fallback", False)
    delta_note     = " ⚠️ delta fallback (flat 0.50 used)" if delta_fallback else ""

    buy_sub    = f"live LTP @ {ftime}"
    tgt_sub    = f"if {instrument} spot → {fi(spot_tgt)}"
    sl_sub     = f"if {instrument} spot → {fi(spot_sl)}  ·  Δ{delta_used:.2f}{delta_note}"

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
        {
            "name":   "Supertrend (10,5)",
            "value":  _supertrend_field_value(direction, result.get("c5")),
            "inline": True,
        },
    ]

    sector_conviction = result.get("sector_conviction")   # None | "HIGH" | "LOW"
    if sector_conviction == "HIGH":
        fields.append({
            "name":   "Sector Signal",
            "value":  "High Conviction with Sector Performance",
            "inline": False,
        })
    elif sector_conviction == "LOW":
        fields.append({
            "name":   "Sector Signal",
            "value":  "Low Conviction with Sector Performance",
            "inline": False,
        })

    return fields


def send_signal(instrument: str, direction: str, result: dict) -> bool:
    """Post a rich Discord embed for a CE or PE signal. Returns True on success."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("[notifier] DISCORD_WEBHOOK_URL not set")
        return False

    is_ce  = direction.upper() == "CE"
    color  = CE_COLOR if is_ce else PE_COLOR
    emoji  = "🟢" if is_ce else "🔴"

    HIGH_CONVICTION_COLOR = 0x3498DB   # blue
    LOW_CONVICTION_COLOR  = 0xE74C3C   # red
    sector_conviction = result.get("sector_conviction")   # None | "HIGH" | "LOW"
    if sector_conviction == "HIGH":
        color = HIGH_CONVICTION_COLOR
    elif sector_conviction == "LOW":
        color = LOW_CONVICTION_COLOR

    fields = _build_trade_fields(instrument, direction, result)

    tag = "🔄 Dynamic Add · " if result.get("is_dynamic") else ""

    embed = {
        "title":     f"{tag}{emoji} {direction.upper()} Signal — {instrument}",
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


def send_suppressed_signal(instrument: str, direction: str, result: dict) -> bool:
    """
    Post a Discord embed for a signal that fired all C1-C4 conditions but
    was suppressed because the ATR-capped target produces an R:R below
    cfg.MIN_RR. Visibility-only — no trade is implied actionable.
    """
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("[notifier] DISCORD_WEBHOOK_URL not set")
        return False

    raw_risk   = result.get("raw_risk")
    target_pts = result.get("target_pts")
    rr         = result.get("rr")
    atr        = result.get("daily_atr")

    fields = _build_trade_fields(instrument, direction, result)
    fields.extend([
        {
            "name":   "Reason",
            "value":  (f"R:R {rr} below minimum {result.get('rr_floor', 0.8)} — "
                       "target capped by ATR/option-range, not worth the risk"),
            "inline": False,
        },
        {
            "name":   "Risk (pts)",
            "value":  f"{raw_risk}" if raw_risk is not None else "—",
            "inline": True,
        },
        {
            "name":   "Capped target (pts)",
            "value":  f"{target_pts}" if target_pts is not None else "—",
            "inline": True,
        },
        {
            "name":   "Daily ATR(14)",
            "value":  f"{atr:.1f}" if atr is not None else "unavailable",
            "inline": True,
        },
    ])

    embed = {
        "title":     f"⚪ {direction.upper()} Signal SUPPRESSED — {instrument} (low R:R)",
        "color":     SUPPRESSED_COLOR,
        "fields":    fields,
        "footer":    {"text": "Visibility only — no trade implied · not logged to journal"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[notifier] Discord returned {resp.status_code}: {resp.text[:200]}")
        else:
            print(f"[notifier] ✓ Discord suppressed-signal sent: {instrument} {direction}")
        return ok
    except Exception as e:
        print(f"[notifier] Discord POST failed: {e}")
        return False


def send_pvwap_bias(date_str: str, bias_data: dict, fib_levels: dict) -> bool:
    """Post the standalone daily PVWAP pre-market bias alert for NIFTY.
    Separate message block — never merged into the per-candle signal embed."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("[notifier] DISCORD_WEBHOOK_URL not set")
        return False

    bias = bias_data.get("bias", "NEUTRAL")
    color = {"CE": CE_COLOR, "PE": PE_COLOR}.get(bias, SUPPRESSED_COLOR)
    support = bias_data.get("support")
    resistance = bias_data.get("resistance")
    fib_618 = fib_levels.get("0.618")

    def fp(v):
        return f"{v:,.1f}" if v is not None else "—"

    embed = {
        "title": "📊 PVWAP Pre-Market Bias — NIFTY",
        "color": color,
        "fields": [
            {"name": "Date", "value": date_str, "inline": True},
            {"name": "Bias", "value": bias, "inline": True},
            {"name": "Key zones", "value": f"Support {fp(support)}, Resistance {fp(resistance)}", "inline": False},
            {"name": "Fib 0.618", "value": fp(fib_618), "inline": True},
            {"name": "Rationale", "value": bias_data.get("rationale", "—"), "inline": True},
        ],
        "footer": {"text": "Alert only · verify before trading"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(webhook, json={"embeds": [embed]}, timeout=10)
        ok = resp.status_code in (200, 204)
        if not ok:
            print(f"[notifier] Discord returned {resp.status_code}: {resp.text[:200]}")
        else:
            print(f"[notifier] ✓ PVWAP bias alert sent: {bias}")
        return ok
    except Exception as e:
        print(f"[notifier] PVWAP bias POST failed: {e}")
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


