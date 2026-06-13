"""
stock_main.py — Stock F&O signal evaluation loop.
Seven Nifty 50 stocks evaluated through C1–C4. Alert-only.
Called by stock-signal.yml every 5 minutes during market hours.

Risk gate: VWAP proximity only (C2). No candle-width gate.
OHLCV: NSE equity tokens (real volume).
Options: NFO monthly chain.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src import calendar_nse, indicators, notifier, state
from src import stock_config as cfg
from src.kite_client import fetch_ohlcv, get_kite

IST = ZoneInfo("Asia/Kolkata")

_DASHBOARD_FILE = "docs/stock-dashboard.json"
_MAX_HISTORY    = 280


# ── Dashboard helpers ─────────────────────────────────────────────────────────

def _load_dashboard() -> dict:
    if not os.path.exists(_DASHBOARD_FILE):
        return _empty_dashboard()
    try:
        with open(_DASHBOARD_FILE) as f:
            data = json.load(f)
        if data.get("date") != date.today().isoformat():
            return _empty_dashboard()
        return data
    except Exception:
        return _empty_dashboard()


def _empty_dashboard() -> dict:
    return {
        "date":           date.today().isoformat(),
        "last_run":       None,
        "token_valid":    True,
        "instruments":    [],
        "active_signals": [],
        "history":        [],
    }


def _commit_dashboard(data: dict) -> None:
    data["last_run"] = datetime.now(IST).isoformat()
    with open(_DASHBOARD_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], check=True)
        subprocess.run(["git", "add", _DASHBOARD_FILE], check=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode != 0:
            ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
            subprocess.run(
                ["git", "commit", "-m", f"stock-dashboard update {ts}"],
                check=True,
            )
            subprocess.run(["git", "push"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"[stock_main] git commit/push failed: {e}")


# ── Signal deduplication ──────────────────────────────────────────────────────

def _is_duplicate(name: str, direction: str, candle_time: str) -> bool:
    """Prefix stock: to avoid collision with index dedup keys."""
    key = f"stock:fired:{name}:{direction}:{candle_time}"
    if state.redis_get(key):
        print(f"[stock_main] {name} {direction}: duplicate — already fired at {candle_time}")
        return True
    state.redis_set(key, "1", ex=86400)
    return False


# ── Option lookup ─────────────────────────────────────────────────────────────

def _get_atm_option(name: str, spot: float, step: int, direction: str) -> dict:
    """
    Retrieve ATM option details from the stock option token cache.
    Returns empty dict on any miss — caller handles gracefully.
    """
    try:
        raw = state.redis_get(cfg.REDIS_OPTION_TOKENS_KEY)
        if not raw:
            print(f"[stock_main] {cfg.REDIS_OPTION_TOKENS_KEY} empty — did morning-login run?")
            return {}

        token_map = json.loads(raw)
        atm       = round(spot / step) * step
        tk_key    = f"{name}_{int(atm)}_{direction}"
        info      = token_map.get(tk_key)
        if not info:
            print(f"[stock_main] ATM token not cached: {tk_key}")
            return {}

        kite     = get_kite()
        opt_key  = f"NFO:{info['tradingsymbol']}"
        ltp_data = kite.ltp([opt_key])
        ltp_val  = ltp_data.get(opt_key, {}).get("last_price")

        return {
            "tradingsymbol": info["tradingsymbol"],
            "strike":        atm,
            "ltp":           round(ltp_val, 2) if ltp_val else None,
            "expiry":        info.get("expiry"),
            "lot_size":      info.get("lot_size"),
            "fetch_time":    datetime.now(IST).strftime("%H:%M:%S IST"),
        }
    except Exception as e:
        print(f"[stock_main] _get_atm_option({name}) failed: {e}")
        return {}


# ── Condition evaluation ─────────────────────────────────────────────────────

def _evaluate(stock: dict, df) -> dict:
    """
    Run C1–C4 on a single stock. Returns a result dict compatible with
    the dashboard JSON schema used by the index bot.

    C2 includes VWAP proximity (sole risk gate — no separate candle-width gate).
    """
    name       = stock["name"]
    step       = stock["strike_step"]
    today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)

    pdi_s, ndi_s, _ = indicators.dmi_wilder(df)
    rsi_s            = indicators.rsi_wilder(df)
    vwap_s           = indicators.vwap_session(df, today_open)

    # Latest CLOSED candle = iloc[-2]; still-forming candle = iloc[-1]
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    spot     = float(curr["close"])
    vwap_now = float(vwap_s.iloc[-2])
    rsi_now  = float(rsi_s.iloc[-2])
    pdi_now  = float(pdi_s.iloc[-2])
    ndi_now  = float(ndi_s.iloc[-2])
    pdi_prev = float(pdi_s.iloc[-3])
    ndi_prev = float(ndi_s.iloc[-3])

    # C1 — momentum (close direction vs prior close)
    ce_c1 = float(curr["close"]) > float(prev["close"])
    pe_c1 = float(curr["close"]) < float(prev["close"])

    # C2 — VWAP cross within 6-candle lookback + proximity filter
    # Proximity is the sole risk gate — mirrors index bot design exactly.
    p_window    = df["close"].iloc[-7:-1].values
    vwap_window = vwap_s.iloc[-7:-1].values
    ce_cross = any(
        p_window[i] > vwap_window[i] and p_window[i - 1] <= vwap_window[i - 1]
        for i in range(1, len(p_window))
    )
    pe_cross = any(
        p_window[i] < vwap_window[i] and p_window[i - 1] >= vwap_window[i - 1]
        for i in range(1, len(p_window))
    )
    prox_ok = abs(spot - vwap_now) <= cfg.VWAP_PROXIMITY_PTS.get(name, 15)
    ce_c2   = ce_cross and prox_ok
    pe_c2   = pe_cross and prox_ok

    # C3 — RSI slope rising/falling over 3 consecutive candles
    rsi_vals = rsi_s.iloc[-5:-1].values
    ce_c3 = len(rsi_vals) >= 3 and all(rsi_vals[i] > rsi_vals[i - 1] for i in range(-3, 0))
    pe_c3 = len(rsi_vals) >= 3 and all(rsi_vals[i] < rsi_vals[i - 1] for i in range(-3, 0))

    # C4 — DMI dominance + rising vs prior candle
    ce_c4 = pdi_now > 25 and pdi_now > ndi_now and pdi_now > pdi_prev
    pe_c4 = ndi_now > 25 and ndi_now > pdi_now and ndi_now > ndi_prev

    ce_signal = ce_c1 and ce_c2 and ce_c3 and ce_c4
    pe_signal = pe_c1 and pe_c2 and pe_c3 and pe_c4

    return {
        "name":             name,
        "sector":           stock["sector"],
        "lot_size":         stock["lot_size"],
        "ce":               {"c1": ce_c1, "c2": ce_c2, "c3": ce_c3, "c4": ce_c4, "signal": ce_signal},
        "pe":               {"c1": pe_c1, "c2": pe_c2, "c3": pe_c3, "c4": pe_c4, "signal": pe_signal},
        "futures_price":    spot,        # equity close; labelled futures_price for dashboard compat
        "candle_high":      float(curr["high"]),
        "candle_low":       float(curr["low"]),
        "prev_candle_high": float(prev["high"]),
        "prev_candle_low":  float(prev["low"]),
        "candle_time":      curr["timestamp"].strftime("%H:%M IST"),
        "vwap":             vwap_now,
        "rsi":              rsi_now,
        "pdi":              pdi_now,
        "ndi":              ndi_now,
        "atm_strike":       round(spot / step) * step,
        "strike_step":      step,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Gate 1: trading day + eval window
    if not (calendar_nse.is_trading_day() and calendar_nse.in_eval_window()):
        print("[stock_main] Outside trading window — exiting")
        return

    # Gate 2: access token present
    token = state.redis_get("kite:access_token")
    if not token:
        notifier.send_warning("⚠️ STOCK BOT: No access token in Redis. Run morning-login.yml.")
        return

    # Load equity tokens (written by morning-login stock caching step)
    raw_equity = state.redis_get(cfg.REDIS_EQUITY_TOKENS_KEY)
    if not raw_equity:
        notifier.send_warning(
            f"⚠️ STOCK BOT: {cfg.REDIS_EQUITY_TOKENS_KEY} missing from Redis. "
            "Did morning-login run with stock caching steps?"
        )
        return
    equity_tokens: dict[str, int] = json.loads(raw_equity)

    today_open         = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    dashboard          = _load_dashboard()
    instrument_results = []
    new_history_rows   = []

    for stock in cfg.STOCKS:
        name = stock["name"]
        try:
            token_id = equity_tokens.get(name)
            if not token_id:
                print(f"[stock_main] ERROR: no equity token for {name} — skipping")
                continue

            df = fetch_ohlcv(token_id, today_open)

            if len(df) < 20:
                print(f"[stock_main] {name}: insufficient candles ({len(df)}) — skipping")
                continue

            result = _evaluate(stock, df)
            instrument_results.append(result)

            ce_signal   = result["ce"]["signal"]
            pe_signal   = result["pe"]["signal"]
            direction   = "CE" if ce_signal else ("PE" if pe_signal else None)
            candle_time = result["candle_time"]

            new_history_rows.append({
                "time":          candle_time,
                "instrument":    name,
                "ce_conditions": [result["ce"]["c1"], result["ce"]["c2"],
                                  result["ce"]["c3"], result["ce"]["c4"]],
                "pe_conditions": [result["pe"]["c1"], result["pe"]["c2"],
                                  result["pe"]["c3"], result["pe"]["c4"]],
                "ce_signal":     ce_signal,
                "pe_signal":     pe_signal,
                "rsi":           result["rsi"],
                "pdi":           result["pdi"],
                "ndi":           result["ndi"],
                "price":         result["futures_price"],
            })

            if not direction:
                print(f"[stock_main] {name}: no signal")
                continue

            # Deduplication
            if _is_duplicate(name, direction, candle_time):
                continue

            # Option data
            opt = _get_atm_option(name, result["futures_price"], stock["strike_step"], direction)

            # SL / target (delta-scaled, same math as index bot)
            spot     = result["futures_price"]
            sl_spot  = result["prev_candle_low"] if direction == "CE" else result["prev_candle_high"]
            risk_pts = abs(spot - sl_spot)
            delta    = 0.50
            from src import config as idx_cfg
            target_rr  = getattr(idx_cfg, "TARGET_RR", 1.5)
            sl_opt     = round(risk_pts * delta, 2)
            target_opt = round(sl_opt * target_rr, 2)

            signal_payload = {
                **result,
                "instrument":    name,
                "direction":     direction,
                "tradingsymbol": opt.get("tradingsymbol"),
                "strike":        opt.get("strike"),
                "atm_ltp":       opt.get("ltp"),
                "expiry":        opt.get("expiry"),
                "opt_sl":        (opt["ltp"] - sl_opt) if opt.get("ltp") else None,
                "opt_target":    (opt["ltp"] + target_opt) if opt.get("ltp") else None,
                "spot_sl":       round(sl_spot, 2),
                "raw_risk":      round(risk_pts, 1),
                "conviction":    "HIGH" if (result["pdi"] > 30 or result["ndi"] > 30) else "MED",
                "rr":            target_rr,
            }

            notifier.send_signal(name, direction, signal_payload)
            print(f"[stock_main] {name}: {direction} SIGNAL FIRED")

        except Exception as e:
            print(f"[stock_main] ERROR processing {name}: {e}")
            continue

    # Update and commit dashboard
    dashboard["instruments"]    = instrument_results
    existing_history            = dashboard.get("history", [])
    dashboard["history"]        = (new_history_rows + existing_history)[:_MAX_HISTORY]
    dashboard["active_signals"] = [
        r for r in new_history_rows if r.get("ce_signal") or r.get("pe_signal")
    ]

    _commit_dashboard(dashboard)
    print(f"[stock_main] Run complete — {len(instrument_results)} stocks evaluated")


if __name__ == "__main__":
    main()
