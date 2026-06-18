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
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src import calendar_nse, config as idx_cfg, indicators, notifier, state
from src import stock_config as cfg
from src.kite_client import fetch_ohlcv, get_kite, get_live_quote

IST = ZoneInfo("Asia/Kolkata")

_DASHBOARD_FILE = "docs/stock-dashboard.json"
_MAX_HISTORY    = 600


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
    from src.git_util import commit_and_push
    data["last_run"] = datetime.now(IST).isoformat()
    with open(_DASHBOARD_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    commit_and_push([_DASHBOARD_FILE], f"stock-dashboard update {ts}")


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

def _live_atm_fallback(name: str, spot: float, step: int, direction: str) -> dict:
    """Resolve ATM contract from live NFO instruments dump when cache misses."""
    try:
        today = date.today()
        kite = get_kite()
        instruments = kite.instruments("NFO")
        atm = round(spot / step) * step
        candidates = [
            i for i in instruments
            if i["name"] == name
            and i["instrument_type"] == direction
            and i["expiry"] >= today
            and i["strike"] == atm
        ]
        if not candidates:
            print(f"[stock_main] live fallback: no {name} {direction} {atm} in NFO")
            return {}
        nearest = min(candidates, key=lambda x: x["expiry"])
        ts = nearest["tradingsymbol"]
        ltp_key = f"NFO:{ts}"
        ltp_data = kite.ltp([ltp_key])
        ltp_val = ltp_data.get(ltp_key, {}).get("last_price")
        return {
            "tradingsymbol": ts,
            "strike":        atm,
            "ltp":           round(ltp_val, 2) if ltp_val else None,
            "expiry":        nearest["expiry"],
            "lot_size":      nearest.get("lot_size"),
            "fetch_time":    datetime.now(IST).strftime("%H:%M:%S IST"),
        }
    except Exception as e:
        print(f"[stock_main] live fallback for {name} failed: {e}")
        return {}


def _get_atm_option(name: str, spot: float, step: int, direction: str) -> dict:
    """
    Retrieve ATM option details from the stock option token cache.
    Falls back to live NFO dump on any miss. Returns empty dict only if live
    resolve also fails.
    """
    try:
        raw = state.redis_get(cfg.REDIS_OPTION_TOKENS_KEY)
        if not raw:
            print(f"[stock_main] {cfg.REDIS_OPTION_TOKENS_KEY} empty — live NFO fallback")
            return _live_atm_fallback(name, spot, step, direction)

        token_map = json.loads(raw)
        atm       = round(spot / step) * step
        tk_key    = f"{name}_{int(atm)}_{direction}"
        info      = token_map.get(tk_key)
        if not info:
            print(f"[stock_main] ATM token not cached: {tk_key} — live NFO fallback")
            return _live_atm_fallback(name, spot, step, direction)

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
    Run C1–C4 on a single stock using live quote + live-recomputed indicators
    against the two most recently closed candles. Raises on a failed live
    quote fetch — the caller's existing per-stock try/except turns that into a
    skip-this-stock-this-run, same as every other failure mode in this loop.
    """
    name       = stock["name"]
    step       = stock["strike_step"]
    today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)

    pdi_s, ndi_s, _ = indicators.dmi_wilder(df)
    rsi_s            = indicators.rsi_wilder(df)
    vwap_s           = indicators.vwap_session(df, today_open)

    p0 = df.iloc[-2]
    p1 = df.iloc[-3]

    v0         = float(vwap_s.iloc[-2])
    r0, r1     = float(rsi_s.iloc[-2]),  float(rsi_s.iloc[-3])
    pdi0, pdi1 = float(pdi_s.iloc[-2]),  float(pdi_s.iloc[-3])
    ndi0, ndi1 = float(ndi_s.iloc[-2]),  float(ndi_s.iloc[-3])

    live_key   = f"{stock['spot_exchange']}:{stock['equity_symbol']}"
    live_quote = get_live_quote(live_key)
    if live_quote is None:
        raise RuntimeError(f"live quote unavailable for {name}")
    live_ltp  = live_quote["ltp"]
    live_vwap = live_quote["vwap"]

    live_df  = indicators.with_live_bar(df, live_ltp)
    live_rsi = float(indicators.rsi_wilder(live_df).iloc[-1])
    live_pdi_s, live_ndi_s, _ = indicators.dmi_wilder(live_df)
    live_pdi = float(live_pdi_s.iloc[-1])
    live_ndi = float(live_ndi_s.iloc[-1])

    di_threshold = cfg.DI_THRESHOLD   # 24

    # C1 — momentum: live price vs P0's close
    ce_c1 = live_ltp > float(p0["close"])
    pe_c1 = live_ltp < float(p0["close"])

    # C2 — VWAP position (live) + P0 dipped/spiked through VWAP
    ce_c2 = live_ltp > live_vwap and float(p0["low"])  <= v0
    pe_c2 = live_ltp < live_vwap and float(p0["high"]) >= v0

    # C3 — RSI direction: live > P0 > P1 (or reverse), no threshold
    ce_c3 = live_rsi > r0 > r1
    pe_c3 = live_rsi < r0 < r1

    # C4 — DI threshold, dominance, direction: live > P0 > P1 (or reverse)
    ce_c4 = live_pdi > di_threshold and live_pdi > live_ndi and live_pdi > pdi0 > pdi1
    pe_c4 = live_ndi > di_threshold and live_ndi > live_pdi and live_ndi > ndi0 > ndi1

    ce_signal = ce_c1 and ce_c2 and ce_c3 and ce_c4
    pe_signal = pe_c1 and pe_c2 and pe_c3 and pe_c4

    return {
        "name":             name,
        "sector":           stock["sector"],
        "lot_size":         stock["lot_size"],
        "ce":               {"c1": ce_c1, "c2": ce_c2, "c3": ce_c3, "c4": ce_c4, "signal": ce_signal},
        "pe":               {"c1": pe_c1, "c2": pe_c2, "c3": pe_c3, "c4": pe_c4, "signal": pe_signal},
        "futures_price":    float(p0["close"]),
        "candle_high":      float(p0["high"]),
        "candle_low":       float(p0["low"]),
        "prev_candle_high": float(p1["high"]),
        "prev_candle_low":  float(p1["low"]),
        "candle_time":      p0["timestamp"].strftime("%H:%M IST"),
        "vwap":             v0,
        "rsi":              r0,
        "pdi":              pdi0,
        "ndi":              ndi0,
        "live_price":       live_ltp,
        "live_vwap":        live_vwap,
        "live_rsi":         live_rsi,
        "live_pdi":         live_pdi,
        "live_ndi":         live_ndi,
        "atm_strike":       round(float(p0["close"]) / step) * step,
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

    # Event exclusion — skip stocks with an upcoming earnings/dividend/corp-action
    # event. Written once daily by morning-login's stock_events.py step; a
    # missing key just means "nothing excluded today."
    excluded_key   = f"{cfg.REDIS_EVENT_EXCLUDED_PREFIX}:{date.today().isoformat()}"
    raw_excluded   = state.redis_get(excluded_key)
    event_excluded: set[str] = set(json.loads(raw_excluded)) if raw_excluded else set()

    now                = datetime.now(IST)
    today_open         = now.replace(hour=9, minute=15, second=0, microsecond=0)
    dashboard          = _load_dashboard()
    instrument_results = []
    new_history_rows   = []
    fired_signals      = []

    target_rr = getattr(idx_cfg, "TARGET_RR", 1.5)

    for stock in cfg.STOCKS:
        name = stock["name"]

        if name in event_excluded:
            print(f"[stock_main] {name}: skipped — event within "
                  f"{cfg.EVENT_LOOKAHEAD_DAYS}d (earnings/dividend/corp action)")
            continue

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
                "time":          now.strftime("%H:%M"),
                "candle_time":   candle_time,
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

            # SL / target (delta-scaled)
            spot     = result["futures_price"]
            sl_spot  = result["prev_candle_low"] if direction == "CE" else result["prev_candle_high"]
            risk_pts = abs(spot - sl_spot)
            dkey     = direction.lower()

            signal_payload = {
                **result,
                "instrument":  name,
                "direction":   direction,
                "asset_class": "STOCK",

                # --- contract identity the NOTIFIER reads (nested) ---
                "atm_data": {
                    "tradingsymbol": opt.get("tradingsymbol"),
                    "strike":        opt.get("strike"),
                    "expiry":        opt.get("expiry"),
                    "fetch_time":    opt.get("fetch_time"),
                },

                # --- premium + spot levels (read top-level by notifier) ---
                "atm_ltp":    opt.get("ltp"),
                "opt_sl":     (opt["ltp"] - round(risk_pts * 0.50, 2))             if opt.get("ltp") else None,
                "opt_target": (opt["ltp"] + round(risk_pts * 0.50 * target_rr, 2)) if opt.get("ltp") else None,
                "spot_ltp":   spot,
                "spot_sl":    round(sl_spot, 2),
                "spot_tgt":   round(spot + risk_pts * target_rr, 2) if direction == "CE"
                              else round(spot - risk_pts * target_rr, 2),
                "raw_risk":   round(risk_pts, 1),

                # --- context fields ---
                "conviction": "HIGH" if (result["pdi"] > 30 or result["ndi"] > 30) else "MED",
                "rr":         target_rr,
                "c1": result[dkey]["c1"], "c2": result[dkey]["c2"],
                "c3": result[dkey]["c3"], "c4": result[dkey]["c4"],
            }

            notifier.send_signal(name, direction, signal_payload)
            print(f"[stock_main] {name}: {direction} SIGNAL FIRED")

            # Rich flat entry for the dashboard drawer
            fired_signals.append({
                "instrument":      name,
                "direction":       direction,
                "candle_time":     result["candle_time"],
                "futures_price":   result["futures_price"],
                "spot_ltp":        spot,
                "fut_spot_spread": None,
                "tradingsymbol":   opt.get("tradingsymbol"),
                "strike":          opt.get("strike"),
                "expiry":          opt.get("expiry"),
                "fetch_time":      opt.get("fetch_time"),
                "atm_ltp":         opt.get("ltp"),
                "opt_target":      signal_payload["opt_target"],
                "opt_sl":          signal_payload["opt_sl"],
                "spot_tgt":        signal_payload["spot_tgt"],
                "spot_sl":         signal_payload["spot_sl"],
                "raw_risk":        signal_payload["raw_risk"],
                "conviction":      signal_payload["conviction"],
                "rr":              target_rr,
                "rsi":             result["rsi"],
                "pdi":             result["pdi"],
                "ndi":             result["ndi"],
                "vwap":            result["vwap"],
                "c1": result[dkey]["c1"], "c2": result[dkey]["c2"],
                "c3": result[dkey]["c3"], "c4": result[dkey]["c4"],
                "asset_class":     "STOCK",
            })

        except Exception as e:
            print(f"[stock_main] ERROR processing {name}: {e}")
            continue

    # Update and commit dashboard
    dashboard["instruments"]    = instrument_results
    existing_history            = dashboard.get("history", [])
    dashboard["history"]        = (new_history_rows + existing_history)[:_MAX_HISTORY]
    dashboard["active_signals"] = fired_signals

    _commit_dashboard(dashboard)
    print(f"[stock_main] Run complete — {len(instrument_results)} stocks evaluated")


if __name__ == "__main__":
    main()
