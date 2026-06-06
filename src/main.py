"""Orchestrator — runs every 5 minutes via signal.yml."""
import json
import os
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


def main() -> None:
    now_ist = datetime.now(IST)
    print(f"[main] Run at {now_ist.isoformat()}")

    # 1. Gate: trading day + eval window
    if not calendar_nse.is_trading_day():
        print("[main] Not a trading day — exiting")
        return
    if not calendar_nse.in_eval_window():
        print(f"[main] Outside eval window {config.EVAL_WINDOW_IST} — exiting")
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

    # 5. Per-instrument loop
    results = []
    cfg = config.as_dict()

    for inst in config.INSTRUMENTS:
        name = inst["name"]
        token_info = instrument_tokens.get(name)
        if not token_info:
            print(f"[main] No token for {name} — skipping")
            continue

        try:
            df = kite_client.fetch_ohlcv(token_info["token"], today_open)
            print(f"[main] {name}: {len(df)} candles fetched")

            vwap = indicators.vwap_session(df, today_open)
            rsi = indicators.rsi_wilder(df)
            pdi, ndi, adx = indicators.dmi_wilder(df)

            inst_cfg = dict(cfg)
            inst_cfg["strike_step"] = inst["strike_step"]

            result = signals.evaluate(df, vwap, rsi, pdi, ndi, inst_cfg)
            result["name"] = name
            result["symbol"] = token_info["tradingsymbol"]
            result["strike_step"] = inst["strike_step"]
            results.append(result)

            rsi_str = f"{result.get('rsi'):.1f}" if result.get("rsi") is not None else "n/a"
            print(
                f"[main] {name}: CE={result['ce']['signal']} PE={result['pe']['signal']} "
                f"price={result.get('futures_price')} rsi={rsi_str}"
            )

            # Signal + dedup + cooldown
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

                    # ── 1. Risk from futures signal candle ───────────────────
                    if dir_up == "CE":
                        raw_risk = max(
                            result["futures_price"] - result["candle_low"],
                            inst["min_risk"],
                        )
                    else:
                        raw_risk = max(
                            result["candle_high"] - result["futures_price"],
                            inst["min_risk"],
                        )

                    # ── 2. Max risk filter ───────────────────────────────────
                    max_r = config.MAX_RISK_POINTS.get(name, 9999)
                    if raw_risk > max_r:
                        print(
                            f"[main] SKIPPED {name} {dir_up} "
                            f"@ {result['candle_time']}: "
                            f"risk {raw_risk:.1f} pts > max {max_r} pts (wide candle)"
                        )
                        continue

                    # ── 3. Fetch live SPOT LTP ───────────────────────────────
                    spot_ltp  = kite_client.get_spot_ltp(name)
                    reference = spot_ltp if spot_ltp is not None \
                                else result["futures_price"]
                    if spot_ltp is None:
                        print(f"[main] {name}: spot LTP unavailable, "
                              f"using futures close as fallback")

                    # ── 4. Conviction label + uniform R:R ────────────────────
                    spread = (result["pdi"] - result["ndi"]) if dir_up == "CE" \
                             else (result["ndi"] - result["pdi"])
                    if   spread >= 18: conv = "Strong"
                    elif spread >= 10: conv = "Moderate"
                    else:              conv = "Building"
                    rr = config.TARGET_RR

                    # ── 5. Spot-anchored index SL and Target ─────────────────
                    if dir_up == "CE":
                        spot_sl  = round(reference - raw_risk,      1)
                        spot_tgt = round(reference + rr * raw_risk, 1)
                    else:
                        spot_sl  = round(reference + raw_risk,      1)
                        spot_tgt = round(reference - rr * raw_risk, 1)

                    # ── 6. Fetch ATM option + live LTP ───────────────────────
                    atm_data = kite_client.get_atm_option(
                        instrument_name=name,
                        spot_price=reference,
                        direction=dir_up,
                        step=inst["strike_step"],
                    )

                    # ── 7. Option premium SL and Target ──────────────────────
                    atm_ltp    = atm_data.get("ltp")
                    opt_sl     = None
                    opt_target = None
                    if atm_ltp is not None:
                        delta      = config.ATM_DELTA
                        opt_sl     = round(atm_ltp - raw_risk * delta,      2)
                        opt_target = round(atm_ltp + raw_risk * rr * delta, 2)

                    # ── 8. Attach everything to result ───────────────────────
                    result["c1"] = result[direction]["c1"]
                    result["c2"] = result[direction]["c2"]
                    result["c3"] = result[direction]["c3"]
                    result["c4"] = result[direction]["c4"]
                    result.update({
                        "spot_ltp":        spot_ltp,
                        "spot_sl":         spot_sl,
                        "spot_tgt":        spot_tgt,
                        "raw_risk":        round(raw_risk, 1),
                        "conviction":      conv,
                        "rr":              rr,
                        "atm_data":        atm_data,
                        "atm_ltp":         atm_ltp,
                        "opt_sl":          opt_sl,
                        "opt_target":      opt_target,
                        "fut_spot_spread": round(result["futures_price"] - reference, 1)
                                           if spot_ltp else None,
                    })

                    # ── 9. Fire alert and record ─────────────────────────────
                    notifier.send_signal(name, dir_up, result)
                    dashboard_writer.update_active_signal(name, dir_up, result)
                    journal.log_signal(name, dir_up, result)
                    state.redis_set(dedup_key, "1", ex=86400)
                    state.redis_set(cooldown_key, candle_ts_str)

        except Exception as e:
            print(f"[main] ERROR processing {name}: {e}")
            # Always continue to next instrument — never crash the loop

    # 6. Update dashboard (every run, signal or not)
    if results:
        dashboard_writer.update_and_commit(results, token_refreshed_at)
    else:
        print("[main] No results — dashboard not updated")


if __name__ == "__main__":
    main()
