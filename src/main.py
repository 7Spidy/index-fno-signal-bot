"""Orchestrator — runs every 5 minutes via signal.yml."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import (
    calendar_nse,
    config,
    dashboard_writer,
    indicators,
    journal,
    kite_client,
    notifier,
    signals,
    state,
)
from src import tracker_bridge
from src.executor_bridge import write_executor_intent

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")


def _within_cooldown(last_fired_str: str | None, candle_ts: object) -> bool:
    """Return True if last_fired is within COOLDOWN_CANDLES × 5 minutes of candle_ts."""
    if not last_fired_str:
        return False
    try:
        from dateutil import parser as dtparser
        last_fired = dtparser.parse(last_fired_str)
        if last_fired.tzinfo is None:
            last_fired = last_fired.replace(tzinfo=IST)
        if hasattr(candle_ts, "tzinfo") and candle_ts.tzinfo is None:
            candle_ts = candle_ts.replace(tzinfo=IST)
        cooldown_seconds = config.COOLDOWN_CANDLES * 5 * 60
        return abs((candle_ts - last_fired).total_seconds()) < cooldown_seconds
    except Exception as e:
        print(f"[main] Cooldown check error: {e}")
        return False


def expected_closed_candle_ts(now_ist: datetime) -> datetime:
    """Label (start time) of the 5-minute candle that should have just closed.

    Candle labels are their start time: the candle covering [10:00, 10:05) is
    labelled 10:00 and closes at 10:05. Shortly after a 5-min boundary this
    returns the label of the candle that just closed.
    """
    floored = now_ist.replace(second=0, microsecond=0)
    floored -= timedelta(minutes=floored.minute % 5)   # most recent 5-min boundary
    return floored - timedelta(minutes=5)


def _latest_closed_ts(df):
    """tz-aware IST timestamp of the latest fully-closed candle (df.iloc[-2]).

    The `timestamp` column from kite_client.fetch_ohlcv is tz-aware IST; this
    localizes defensively in case it ever comes back tz-naive.
    """
    ts = df.iloc[-2]["timestamp"]
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize(IST) if hasattr(ts, "tz_localize") else ts.replace(tzinfo=IST)
    return ts


def _evaluate_instrument(inst, token_info, live_quotes, today_open, now_ist, cfg):
    """Fetch candles, compute indicators, and run signal evaluation for one
    instrument. Pure read + compute — no Redis writes, no Discord, no git.
    Safe to run concurrently across instruments. Returns (result, df) on
    success, or None if this instrument should be skipped this run."""
    name = inst["name"]
    try:
        df = kite_client.fetch_ohlcv(token_info["token"], today_open)
        print(f"[main] {name}: {len(df)} candles fetched")

        if len(df) < 2:
            print(f"[main] {name}: <2 candles — skip")
            return None
        expected_ts = expected_closed_candle_ts(now_ist)
        closed_ts = _latest_closed_ts(df)
        if closed_ts < expected_ts:
            print(f"[main] {name}: candle lag (have {closed_ts}, want {expected_ts}) — retry in 5s")
            time.sleep(5)
            df = kite_client.fetch_ohlcv(token_info["token"], today_open)
            if len(df) < 2:
                print(f"[main] {name}: <2 candles after retry — skip")
                return None
            closed_ts = _latest_closed_ts(df)
        if closed_ts != expected_ts:
            print(f"[main] {name}: stale candle (have {closed_ts}, want {expected_ts}) — skip")
            return None

        vwap = indicators.vwap_session(df, today_open)
        rsi = indicators.rsi_wilder(df)
        pdi, ndi, _ = indicators.dmi_wilder(df)

        exchange = inst.get("fno_exchange", "NFO")
        live_key = f"{exchange}:{token_info['tradingsymbol']}"
        live_quote = live_quotes.get(live_key)
        if live_quote is None:
            print(f"[main] {name}: live quote unavailable — skipping this run")
            return None

        live_df = indicators.with_live_bar(df, live_quote["ltp"])
        live_rsi_s = indicators.rsi_wilder(live_df)
        live_pdi_s, live_ndi_s, _ = indicators.dmi_wilder(live_df)
        _, live_st_dir_s = indicators.supertrend_wilder(
            live_df, cfg["SUPERTREND_PERIOD"], cfg["SUPERTREND_MULTIPLIER"]
        )
        _live_dir_val = live_st_dir_s.iloc[-1]
        live_supertrend_dir = bool(_live_dir_val) if _live_dir_val is not None else None

        inst_cfg = dict(cfg)
        inst_cfg["strike_step"] = inst["strike_step"]
        inst_cfg["instrument_name"] = name

        result = signals.evaluate(
            df, vwap, rsi, pdi, ndi, inst_cfg,
            live_ltp=live_quote["ltp"],
            live_vwap=live_quote["vwap"],
            live_rsi=float(live_rsi_s.iloc[-1]),
            live_pdi=float(live_pdi_s.iloc[-1]),
            live_ndi=float(live_ndi_s.iloc[-1]),
            live_supertrend_dir=live_supertrend_dir,
        )
        result["name"] = name
        result["symbol"] = token_info["tradingsymbol"]
        result["strike_step"] = inst["strike_step"]
        return (result, df)

    except Exception as e:
        print(f"[main] ERROR processing {name}: {e}")
        return None


def main() -> None:
    now_ist = datetime.now(IST)
    print(f"[main] Run at {now_ist.isoformat()}")

    # 1. Gate: trading day (global — no instruments trade on a holiday)
    if not calendar_nse.is_trading_day():
        print("[main] Not a trading day — exiting")
        return

    # 2. Read access token
    token = state.redis_get("kite:access_token")
    if not token:
        notifier.send_warning("⚠️ No access token in Redis. Run morning-login.yml.")
        return

    # 3. Read instrument tokens (cached by morning-login)
    raw = state.redis_get("kite:instrument_tokens")
    if raw:
        instrument_tokens = json.loads(raw)
    else:
        print("[main] Instrument tokens not cached — resolving now...")
        instrument_tokens = kite_client.resolve_futures_tokens()

    token_refreshed_at = state.redis_get("kite:token_refreshed_at")

    # 4. Session open for today (09:15 IST)
    today_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)

    # 4.5. Batch-fetch live quotes for all instruments in ONE Kite API call
    live_keys = []
    for inst in config.INSTRUMENTS:
        token_info = instrument_tokens.get(inst["name"])
        if not token_info:
            continue
        exchange = inst.get("fno_exchange", "NFO")
        live_keys.append(f"{exchange}:{token_info['tradingsymbol']}")
    live_quotes = kite_client.get_live_quotes_batch(live_keys)

    # 5. Per-instrument evaluation. Fetch + indicator compute run
    # concurrently across instruments (bounded by Kite's 3 req/sec limit
    # inside fetch_ohlcv via _throttle_historical_call). Signal firing below
    # stays strictly sequential, in original instrument order, so dedup/
    # cooldown/Discord/git behavior is unchanged.
    results = []
    cfg = config.as_dict()

    pending = []
    for inst in config.INSTRUMENTS:
        name = inst["name"]
        if not calendar_nse.in_eval_window_for(name):
            print(f"[main] {name}: outside eval window "
                  f"(expiry-day cutoff applies: {calendar_nse.is_expiry_day(name)})")
            continue
        token_info = instrument_tokens.get(name)
        if not token_info:
            print(f"[main] No token for {name} — skipping")
            continue
        pending.append((inst, token_info))

    outcomes = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_name = {
            pool.submit(
                _evaluate_instrument, inst, token_info, live_quotes,
                today_open, now_ist, cfg,
            ): inst["name"]
            for inst, token_info in pending
        }
        for future in future_to_name:
            outcomes[future_to_name[future]] = future.result()

    for inst, token_info in pending:
        name = inst["name"]
        outcome = outcomes.get(name)
        if outcome is None:
            continue
        result, df = outcome
        results.append(result)

        rsi_str = f"{result.get('rsi'):.1f}" if result.get("rsi") is not None else "n/a"
        vwap_val = result.get("vwap")
        vwap_gap = (
            f"{abs(result.get('futures_price', 0) - vwap_val):.1f}pts from VWAP"
            if vwap_val else "vwap=n/a"
        )
        print(
            f"[main] {name}: CE={result['ce']['signal']} PE={result['pe']['signal']} "
            f"price={result.get('futures_price')} rsi={rsi_str} {vwap_gap}"
        )

        # Signal + dedup + cooldown — wrapped in its own try/except so a
        # failure firing one instrument's alert can never crash the loop or
        # block the remaining instruments, matching the original guarantee.
        try:
            for direction in ("ce", "pe"):
                if result[direction]["signal"]:
                    raw_ts = df.iloc[-2]["timestamp"]
                    candle_ts = raw_ts
                    candle_ts_str = raw_ts.isoformat() if hasattr(raw_ts, "isoformat") else str(raw_ts)
                    dedup_key = f"fired:{name}:{direction}:{candle_ts_str}"

                    if state.redis_exists(dedup_key):
                        print(f"[main] {name} {direction.upper()} already fired this candle — dedup skip")
                        continue

                    cooldown_key = f"cooldown:{name}:{direction}"
                    last_fired = state.redis_get(cooldown_key)
                    if _within_cooldown(last_fired, candle_ts):
                        print(f"[main] {name} {direction.upper()} in cooldown — skip")
                        continue

                    dir_up = direction.upper()

                    spot_ltp  = kite_client.get_spot_ltp(name)
                    reference = spot_ltp if spot_ltp is not None \
                                else result["futures_price"]
                    if spot_ltp is None:
                        print(f"[main] {name}: spot LTP unavailable, "
                              f"using futures close as fallback")

                    spread = (result["pdi"] - result["ndi"]) if dir_up == "CE" \
                             else (result["ndi"] - result["pdi"])
                    if   spread >= 18: conv = "Strong"
                    elif spread >= 10: conv = "Moderate"
                    else:              conv = "Building"
                    rr = config.TARGET_RR

                    if dir_up == "CE":
                        spot_sl  = round(result["prev_candle_low"],  1)
                        raw_risk = max(reference - spot_sl, inst["min_risk"])
                        spot_tgt = round(reference + rr * raw_risk,  1)
                    else:
                        spot_sl  = round(result["prev_candle_high"], 1)
                        raw_risk = max(spot_sl - reference, inst["min_risk"])
                        spot_tgt = round(reference - rr * raw_risk,  1)

                    atm_data = kite_client.get_atm_option(
                        instrument_name=name,
                        spot_price=reference,
                        direction=dir_up,
                        step=inst["strike_step"],
                    )

                    atm_ltp    = atm_data.get("ltp")
                    opt_sl     = None
                    opt_target = None
                    delta      = kite_client.estimate_atm_delta(
                        instrument_name=name,
                        atm_strike=atm_data.get("strike") or result["atm_strike"],
                        direction=dir_up,
                        step=inst["strike_step"],
                    )
                    if atm_ltp is not None:
                        opt_sl     = round(atm_ltp - raw_risk * delta,      2)
                        opt_target = round(atm_ltp + raw_risk * rr * delta, 2)

                    result["c1"] = result[direction]["c1"]
                    result["c2"] = result[direction]["c2"]
                    result["c3"] = result[direction]["c3"]
                    result["c4"] = result[direction]["c4"]
                    result["c5"] = result[direction]["c5"]
                    result.update({
                        "spot_ltp":        spot_ltp,
                        "spot_sl":         spot_sl,
                        "spot_tgt":        spot_tgt,
                        "raw_risk":        round(raw_risk, 1),
                        "atm_delta":       delta,
                        "conviction":      conv,
                        "rr":              rr,
                        "atm_data":        atm_data,
                        "atm_ltp":         atm_ltp,
                        "opt_sl":          opt_sl,
                        "opt_target":      opt_target,
                        "fut_spot_spread": round(result["futures_price"] - reference, 1)
                                           if spot_ltp else None,
                    })

                    notifier.send_signal(name, dir_up, result)
                    try:
                        write_executor_intent(result, inst)
                    except Exception as e:
                        print(f"[main] executor_bridge error (non-fatal): {e}")
                    try:
                        tracker_bridge.write_tracker_intent(
                            instrument=name,
                            asset_class="INDEX",
                            direction=dir_up,
                            tradingsymbol=result.get("atm_data", {}).get("tradingsymbol"),
                            spot_sl=result.get("spot_sl"),
                            target_pts=result["raw_risk"] * result["rr"],
                            spot_risk_pts=result.get("raw_risk"),
                            target_rr=result.get("rr"),
                            target_source="rr",
                            atm_strike=result.get("atm_strike"),
                        )
                    except Exception as e:
                        print(f"[main] tracker_bridge error (non-fatal): {e}")
                    dashboard_writer.update_active_signal(name, dir_up, result)
                    journal.log_signal(name, dir_up, result)
                    state.redis_set(dedup_key, "1", ex=86400)
                    state.redis_set(cooldown_key, candle_ts_str)

        except Exception as e:
            print(f"[main] ERROR firing signal for {name}: {e}")
            # Always continue to next instrument — never crash the loop

    # 6. Update dashboard (every run, signal or not)
    if results:
        dashboard_writer.update_and_commit(results, token_refreshed_at)
    else:
        print("[main] No results — dashboard not updated")


if __name__ == "__main__":
    main()
