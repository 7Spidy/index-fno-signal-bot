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

            print(
                f"[main] {name}: CE={result['ce']['signal']} PE={result['pe']['signal']} "
                f"price={result.get('futures_price')} rsi={result.get('rsi'):.1f if result.get('rsi') else 'n/a'}"
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

                    notifier.send_signal(name, direction.upper(), result)
                    journal.log_signal(name, direction.upper(), result)
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
