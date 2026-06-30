"""
stock_main.py — Stock F&O signal evaluation loop.
14 NSE-listed stocks evaluated through C1–C4. Alert-only.
Called by stock-signal.yml every 5 minutes during market hours.

Risk gate: VWAP proximity only (C2). No candle-width gate.
OHLCV: NSE equity tokens (real volume).
Options: NFO monthly chain.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src import calendar_nse, indicators, notifier, sector_config, state
from src import stock_config as cfg
from src.kite_client import fetch_ohlcv, get_kite, get_live_quotes_batch

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
    """Resolve ATM contract from live NFO instruments dump when cache misses.
    Applies the same monthly rollover check as _get_nearest_monthly_expiry()."""
    try:
        today = date.today()
        kite  = get_kite()
        instruments = kite.instruments("NFO")
        atm = round(spot / step) * step

        # Compute rollover-aware expiry (same logic as stock_kite_client)
        all_expiries = sorted({
            i["expiry"] for i in instruments
            if i.get("name") == name
            and i.get("instrument_type") in ("CE", "PE")
            and i.get("expiry") and i["expiry"] >= today
        })
        if not all_expiries:
            print(f"[stock_main] live fallback: no expiries for {name} in NFO")
            return {}

        candidate = all_expiries[0]
        next_td   = today + timedelta(days=1)
        while not calendar_nse.is_trading_day(next_td):
            next_td += timedelta(days=1)

        if candidate == today or candidate == next_td:
            if len(all_expiries) > 1:
                resolved_expiry = all_expiries[1]
            else:
                print(f"[stock_main] WARNING: {name} rollover wanted but "
                      f"only one expiry in dump — using {candidate}")
                resolved_expiry = candidate
        else:
            resolved_expiry = candidate

        rolled = (resolved_expiry != candidate)

        candidates = [
            i for i in instruments
            if i["name"] == name
            and i["instrument_type"] == direction
            and i["expiry"] == resolved_expiry
            and i["strike"] == atm
        ]
        if not candidates:
            print(f"[stock_main] live fallback: no {name} {direction} {atm} "
                  f"in NFO for expiry {resolved_expiry}")
            return {}
        nearest = candidates[0]
        ts = nearest["tradingsymbol"]
        ltp_key = f"NFO:{ts}"
        ltp_data = kite.ltp([ltp_key])
        ltp_val = ltp_data.get(ltp_key, {}).get("last_price")
        return {
            "tradingsymbol":  ts,
            "strike":         atm,
            "ltp":            round(ltp_val, 2) if ltp_val else None,
            "expiry":         nearest["expiry"],
            "lot_size":       nearest.get("lot_size"),
            "fetch_time":     datetime.now(IST).strftime("%H:%M:%S IST"),
            "rolled_forward": rolled,
        }
    except Exception as e:
        print(f"[stock_main] live fallback for {name} failed: {e}")
        return {}


def _compute_moneyness_pct(spot: float, strike: float, direction: str) -> float:
    """
    Signed % distance between spot and strike, oriented so positive = ITM
    for the given direction. Raises on bad input — caller handles fallback.
    """
    if not spot or not strike:
        raise ValueError("missing spot or strike for moneyness calc")
    if direction == "CE":
        return (spot - strike) / strike * 100
    else:  # PE
        return (strike - spot) / strike * 100


def _lookup_delta(spot: float, strike: float, direction: str) -> tuple[float, bool]:
    """
    Returns (delta, used_fallback). Walks cfg.DELTA_MONEYNESS_BUCKETS
    ascending and returns the first bucket whose upper bound covers the
    computed moneyness. Falls back to cfg.DELTA_FALLBACK on any error.
    """
    try:
        moneyness_pct = _compute_moneyness_pct(spot, strike, direction)
        for upper_bound, delta in cfg.DELTA_MONEYNESS_BUCKETS:
            if moneyness_pct <= upper_bound:
                return delta, False
        return cfg.DELTA_FALLBACK, True  # should not happen (inf bucket covers all)
    except Exception as e:
        print(f"[stock_main] delta lookup failed, using fallback {cfg.DELTA_FALLBACK}: {e}")
        return cfg.DELTA_FALLBACK, True


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
            print(f"[stock_main] ATM token not cached: {tk_key} — refreshing cache")
            from src.stock_kite_client import cache_stock_option_tokens
            cache_stock_option_tokens()
            raw = state.redis_get(cfg.REDIS_OPTION_TOKENS_KEY)
            token_map = json.loads(raw) if raw else {}
            info = token_map.get(tk_key)
        if not info:
            print(f"[stock_main] ATM token still not cached after refresh: "
                  f"{tk_key} — live NFO fallback")
            return _live_atm_fallback(name, spot, step, direction)

        kite     = get_kite()
        opt_key  = f"NFO:{info['tradingsymbol']}"
        ltp_data = kite.ltp([opt_key])
        ltp_val  = ltp_data.get(opt_key, {}).get("last_price")

        return {
            "tradingsymbol":  info["tradingsymbol"],
            "strike":         atm,
            "ltp":            round(ltp_val, 2) if ltp_val else None,
            "expiry":         info.get("expiry"),
            "lot_size":       info.get("lot_size"),
            "fetch_time":     datetime.now(IST).strftime("%H:%M:%S IST"),
            "rolled_forward": info.get("rolled_forward", False),
        }
    except Exception as e:
        print(f"[stock_main] _get_atm_option({name}) failed: {e}")
        return {}


# ── Condition evaluation ─────────────────────────────────────────────────────

def _evaluate(stock: dict, df, live_quotes: dict) -> dict:
    """
    Run C1–C4 on a single stock using a pre-fetched live quote (from the
    single batched kite.quote() call made once per run in main()) plus
    live-recomputed indicators against the two most recently closed candles.
    Raises if this stock's key is missing from live_quotes — the caller's
    existing per-stock try/except turns that into a skip-this-stock-this-run,
    same as every other failure mode in this loop.
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
    live_quote = live_quotes.get(live_key)
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

def _fetch_and_evaluate(stock, equity_tokens, live_quotes, today_open):
    """Fetch candles and run C1–C4 for one stock. Pure read + compute — no
    Redis writes, no Discord, no git. Safe to run concurrently across
    stocks. Returns (stock, result, df) on success, or None if this stock
    should be skipped this run."""
    name = stock["name"]
    try:
        token_id = equity_tokens.get(name)
        if not token_id:
            print(f"[stock_main] ERROR: no equity token for {name} — skipping")
            return None

        df = fetch_ohlcv(token_id, today_open)

        if len(df) < 20:
            print(f"[stock_main] {name}: insufficient candles ({len(df)}) — skipping")
            return None

        result = _evaluate(stock, df, live_quotes)
        return (stock, result, df)

    except Exception as e:
        print(f"[stock_main] ERROR processing {name}: {e}")
        return None


def main() -> None:
    # Gate 1: trading day (global — no instruments trade on a holiday)
    if not calendar_nse.is_trading_day():
        print("[stock_main] Not a trading day — exiting")
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

    # Sector-relative-strength conviction tag — informational only. Missing key
    # (morning step failed, or sector unresolved) = no tag, never a blocker.
    sector_perf_key = f"{cfg.REDIS_SECTOR_PERF_PREFIX}:{date.today().isoformat()}"
    raw_sector_perf = state.redis_get(sector_perf_key)
    sector_perf: dict = json.loads(raw_sector_perf) if raw_sector_perf else {}

    now                = datetime.now(IST)
    today_open         = now.replace(hour=9, minute=15, second=0, microsecond=0)
    dashboard          = _load_dashboard()
    instrument_results = []
    new_history_rows   = []
    fired_signals      = []

    raw_daily_atr = state.redis_get(cfg.REDIS_DAILY_ATR_KEY)
    daily_atr_map: dict[str, float] = json.loads(raw_daily_atr) if raw_daily_atr else {}
    if not daily_atr_map:
        print("[stock_main] WARNING — no cached daily ATR, falling back to "
              "flat 1.5R target for all stocks this run")

    # Batch-fetch live quotes for all 12 stocks in ONE Kite API call instead
    # of one call per stock — sidesteps the quote endpoint's 1 req/sec limit
    # entirely regardless of how many stocks are in cfg.STOCKS.
    live_keys   = [f"{s['spot_exchange']}:{s['equity_symbol']}" for s in cfg.STOCKS]
    live_quotes = get_live_quotes_batch(live_keys)

    # Per-stock fetch + evaluation runs concurrently (bounded by Kite's 3
    # req/sec limit inside fetch_ohlcv). History rows, dedup, and alert
    # firing below stay strictly sequential and in original stock order.
    pending = []
    for stock in cfg.STOCKS:
        name = stock["name"]
        if name in event_excluded:
            print(f"[stock_main] {name}: skipped — event within "
                  f"{cfg.EVENT_LOOKAHEAD_DAYS}d (earnings/dividend/corp action)")
            continue
        if not calendar_nse.in_eval_window_for(name):
            print(f"[stock_main] {name}: outside eval window "
                  f"(expiry-day cutoff applies: {calendar_nse.is_expiry_day(name)})")
            continue
        pending.append(stock)

    outcomes = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_name = {
            pool.submit(_fetch_and_evaluate, stock, equity_tokens, live_quotes, today_open): stock["name"]
            for stock in pending
        }
        for future in future_to_name:
            outcomes[future_to_name[future]] = future.result()

    for stock in pending:
        name = stock["name"]
        outcome = outcomes.get(name)
        if outcome is None:
            continue
        _, result, df = outcome

        try:
            instrument_results.append(result)

            ce_signal   = result["ce"]["signal"]
            pe_signal   = result["pe"]["signal"]
            direction   = "CE" if ce_signal else ("PE" if pe_signal else None)
            candle_time = result["candle_time"]

            if not direction:
                print(f"[stock_main] {name}: no signal")
                continue

            # History log only records candles where a signal actually fired —
            # "no signal" rows are no longer written (was every 5-min check
            # before). The "current status" cards above are unaffected.
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

            # Deduplication
            if _is_duplicate(name, direction, candle_time):
                continue

            # Option data
            opt = _get_atm_option(name, result["futures_price"], stock["strike_step"], direction)

            # SL (unchanged — anchored to prior candle structural extreme)
            spot     = result["futures_price"]
            sl_spot  = result["prev_candle_low"] if direction == "CE" else result["prev_candle_high"]
            risk_pts = abs(spot - sl_spot)
            dkey     = direction.lower()

            delta_used, delta_fallback = _lookup_delta(
                spot, opt.get("strike"), direction
            )

            # Target: ATR-anchored, decoupled from 1.5R. Falls back to the
            # old flat-1.5R formula only if this stock's ATR is missing from
            # the cache (e.g. morning-login ATR step failed or stock was
            # newly added without a cache refresh yet).
            stock_atr = daily_atr_map.get(name)
            if stock_atr:
                raw_target_pts = cfg.ATR_TARGET_K * stock_atr
                floor_pts      = max(0.0015 * spot, 2 * cfg.SLIPPAGE_PTS_EST)
                ceiling_pts    = 0.8 * cfg.OPTION_CACHE_RANGE[name]
                target_pts     = max(floor_pts, min(raw_target_pts, ceiling_pts))
                rr_effective   = round(target_pts / risk_pts, 2) if risk_pts else 0
                target_source  = "atr"
            else:
                target_pts    = risk_pts * 1.5
                rr_effective  = 1.5
                target_source = "fallback_1.5R"

            rr_suppressed = stock_atr is not None and rr_effective < cfg.MIN_RR

            sector_key  = sector_config.STOCK_SECTOR.get(name)
            sector_data = sector_perf.get(sector_key) if sector_key else None

            conviction_tag = None
            if sector_data and sector_data["tag"] != "NEUTRAL":
                is_ce      = direction == "CE"
                sector_out = sector_data["tag"] == "OUT"
                if (is_ce and sector_out) or (not is_ce and not sector_out):
                    conviction_tag = "HIGH"
                else:
                    conviction_tag = "LOW"

            signal_payload = {
                **result,
                "instrument":        name,
                "direction":         direction,
                "asset_class":       "STOCK",
                "sector_conviction": conviction_tag,
                "atm_data": {
                    "tradingsymbol":  opt.get("tradingsymbol"),
                    "strike":         opt.get("strike"),
                    "expiry":         opt.get("expiry"),
                    "fetch_time":     opt.get("fetch_time"),
                    "rolled_forward": opt.get("rolled_forward", False),
                },
                "atm_ltp":        opt.get("ltp"),
                "opt_sl":         (opt["ltp"] - round(risk_pts  * delta_used, 2))          if opt.get("ltp") else None,
                "opt_target":     (opt["ltp"] + round(target_pts * delta_used, 2))          if opt.get("ltp") else None,
                "delta_used":     delta_used,
                "delta_fallback": delta_fallback,
                "spot_ltp":      spot,
                "spot_sl":       round(sl_spot, 2),
                "spot_tgt":      round(spot + target_pts, 2) if direction == "CE"
                                 else round(spot - target_pts, 2),
                "raw_risk":      round(risk_pts, 1),
                "target_pts":    round(target_pts, 1),
                "target_source": target_source,
                "daily_atr":     stock_atr,
                "conviction":    "HIGH" if (result["pdi"] > 30 or result["ndi"] > 30) else "MED",
                "rr":            rr_effective,
                "c1": result[dkey]["c1"], "c2": result[dkey]["c2"],
                "c3": result[dkey]["c3"], "c4": result[dkey]["c4"],
            }

            if rr_suppressed:
                notifier.send_suppressed_signal(name, direction, signal_payload)
                print(f"[stock_main] {name}: {direction} SIGNAL SUPPRESSED "
                      f"(RR {rr_effective} < {cfg.MIN_RR})")
                continue   # do not log to history/journal as a fired trade

            notifier.send_signal(name, direction, signal_payload)
            print(f"[stock_main] {name}: {direction} SIGNAL FIRED "
                  f"(target={target_source}, RR={rr_effective})")

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
                "target_pts":      signal_payload["target_pts"],
                "target_source":   signal_payload["target_source"],
                "daily_atr":       signal_payload["daily_atr"],
                "conviction":      signal_payload["conviction"],
                "delta_used":      signal_payload["delta_used"],
                "delta_fallback":  signal_payload["delta_fallback"],
                "rr":              signal_payload["rr"],
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
