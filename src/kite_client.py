"""Kite Connect client: session bootstrap, instrument resolver, OHLCV fetcher."""
import json
import os
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect

from src import state, config

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")

# Per-process cache so each exchange dump (NFO, BFO) is fetched at most once per run.
_INSTRUMENTS_BY_EXCHANGE: dict[str, list] = {}


def _instruments_for(kite: KiteConnect, exchange: str) -> list:
    """Fetch and cache the full instrument list for an exchange segment."""
    if exchange not in _INSTRUMENTS_BY_EXCHANGE:
        print(f"[kite] Fetching {exchange} instrument list...")
        _INSTRUMENTS_BY_EXCHANGE[exchange] = kite.instruments(exchange)
    return _INSTRUMENTS_BY_EXCHANGE[exchange]


_KITE_SINGLETON: KiteConnect | None = None
_KITE_LOCK = threading.Lock()


def get_kite() -> KiteConnect:
    """Returns a cached KiteConnect client for this process. The access token
    is read from Redis once per process lifetime (one GHA run = one process),
    not on every call site. A failed lookup never poisons the cache — only a
    successful token fetch is stored. Double-checked lock: safe to call from
    multiple threads (the per-instrument fetch loop now runs concurrently)."""
    global _KITE_SINGLETON
    if _KITE_SINGLETON is not None:
        return _KITE_SINGLETON

    with _KITE_LOCK:
        if _KITE_SINGLETON is not None:
            return _KITE_SINGLETON
        api_key = os.environ["KITE_API_KEY"]
        kite = KiteConnect(api_key=api_key)
        token = state.redis_get("kite:access_token")
        if not token:
            raise RuntimeError("No access token in Redis. Run morning-login.yml first.")
        kite.set_access_token(token)
        _KITE_SINGLETON = kite
        return kite


def resolve_futures_tokens(kite: KiteConnect | None = None) -> dict:
    if kite is None:
        kite = get_kite()

    result = {}
    for inst in config.INSTRUMENTS:
        underlying = inst["name"]
        exchange   = inst.get("fno_exchange", "NFO")
        instruments = _instruments_for(kite, exchange)
        futs = [i for i in instruments
                if i["name"] == underlying
                and i["instrument_type"] == "FUT"
                and i["expiry"] >= date.today()]
        if not futs:
            print(f"[kite] WARNING: No futures found for {underlying} on {exchange}")
            continue
        nearest = min(futs, key=lambda x: x["expiry"])
        result[underlying] = {
            "token":         nearest["instrument_token"],
            "expiry":        nearest["expiry"].isoformat(),
            "tradingsymbol": nearest["tradingsymbol"],
            "strike_step":   inst["strike_step"],
        }
        print(f"[kite]   {underlying}: {nearest['tradingsymbol']} (token={nearest['instrument_token']})")

    state.redis_set("kite:instrument_tokens", json.dumps(result), ex=86400)
    print(f"[kite] ✓ Instrument tokens cached for {len(result)} instruments")
    return result


def get_spot_ltp(instrument_name: str) -> float | None:
    """Fetches the live LTP of the spot index right now."""
    try:
        token = config.SPOT_TOKENS.get(instrument_name)
        if not token:
            print(f"[kite_client] No spot token for {instrument_name}")
            return None
        exchange = config.SPOT_EXCHANGE.get(instrument_name, "NSE")
        kite = get_kite()
        key  = f"{exchange}:{token}"
        data = kite.ltp([key])
        return data[key]["last_price"]
    except Exception as e:
        print(f"[kite_client] get_spot_ltp({instrument_name}) failed: {e}")
        return None


def get_nearest_expiry(instrument_name: str) -> tuple[date, bool]:
    """Active option expiry, with rollover applied. Returns (expiry, rolled_forward).

    Weekly instruments (USE_WEEKLY=True): if the nearest expiry is today,
    roll to the next available weekly expiry — checked from market open,
    not gated by time of day.

    Monthly instruments (USE_WEEKLY=False): if the nearest expiry is today
    OR the next NSE trading day, roll to the next monthly expiry.
    """
    import json as _json
    from src.state import redis_get, redis_set
    now_ist = datetime.now(IST)
    today   = now_ist.date()

    cache_key = f"kite:expiry:{instrument_name}"
    cached = redis_get(cache_key)
    if cached:
        try:
            # New format: JSON {"date": "...", "rolled": bool}
            # Old format: plain ISO date string — treat rolled as False.
            try:
                c      = _json.loads(cached)
                exp    = date.fromisoformat(c["date"])
                rolled = bool(c.get("rolled", False))
            except (ValueError, KeyError, TypeError):
                exp    = date.fromisoformat(cached)
                rolled = False
            if exp >= today:
                return exp, rolled
        except ValueError:
            pass

    # ── Primary: live Kite dump (authoritative on holiday shifts) ──
    try:
        kite        = get_kite()
        exchange    = config.fno_exchange_for(instrument_name)
        instruments = _instruments_for(kite, exchange)
        opts = [i for i in instruments
                if i["name"] == instrument_name
                and i["instrument_type"] in ("CE", "PE")
                and i["expiry"] >= today]
        if not opts:
            raise ValueError(f"no options for {instrument_name} on {exchange}")

        distinct_expiries = sorted({o["expiry"] for o in opts})
        candidate = distinct_expiries[0]

        is_weekly = config.USE_WEEKLY.get(instrument_name, False)
        if is_weekly:
            if candidate == today:
                if len(distinct_expiries) > 1:
                    exp = distinct_expiries[1]
                else:
                    print(f"[kite_client] WARNING: {instrument_name} weekly rollover "
                          f"wanted but only one expiry in dump — using {candidate}")
                    exp = candidate
            else:
                exp = candidate
        else:
            # Monthly: roll if today OR the next NSE trading day is expiry
            from src import calendar_nse
            next_td = today + timedelta(days=1)
            while not calendar_nse.is_trading_day(next_td):
                next_td += timedelta(days=1)
            if candidate == today or candidate == next_td:
                if len(distinct_expiries) > 1:
                    exp = distinct_expiries[1]
                else:
                    print(f"[kite_client] WARNING: {instrument_name} monthly rollover "
                          f"wanted but only one expiry in dump — using {candidate}")
                    exp = candidate
            else:
                exp = candidate

        rolled = (exp != candidate)
        redis_set(cache_key,
                  _json.dumps({"date": exp.isoformat(), "rolled": rolled}),
                  ex=21600)
        print(f"[kite_client] {instrument_name} nearest expiry (live): {exp}"
              + (" [rolled forward]" if rolled else ""))
        return exp, rolled

    except Exception as e:
        print(f"[kite_client] live expiry resolve failed for {instrument_name}: {e}")

    # ── Calendar-aware fallback (no Kite): weekly instruments ──
    weekday = config.WEEKLY_EXPIRY_WEEKDAY.get(instrument_name)
    if weekday is not None:
        from src import calendar_nse
        days_ahead = (weekday - today.weekday()) % 7
        cand = today + timedelta(days=days_ahead)
        while not calendar_nse.is_trading_day(cand):   # holiday → previous trading day
            cand -= timedelta(days=1)
        if cand == today:
            print(f"[kite_client] WARNING: {instrument_name} rollover would apply "
                  f"(expiry day) but live dump unavailable — using today's expiry")
        print(f"[kite_client] {instrument_name} nearest expiry (calendar fallback): {cand}")
        return cand, False

    # ── Monthly fallback ──
    import calendar as _cal
    last = _cal.monthrange(today.year, today.month)[1]
    cand = date(today.year, today.month, last)
    print(f"[kite_client] {instrument_name} nearest expiry (monthly calendar fallback): {cand}")
    return cand, False


def cache_option_tokens() -> None:
    """Called once from morning-login. Fetches instrument lists per exchange and stores
    a compact token map in Redis (key 'kite:option_tokens', TTL 24 hours)."""
    try:
        kite   = get_kite()
        result = {}

        for inst_cfg in config.INSTRUMENTS:
            name     = inst_cfg["name"]
            exchange = inst_cfg.get("fno_exchange", "NFO")
            instruments = _instruments_for(kite, exchange)
            spot          = get_spot_ltp(name) or 0
            expiry, rolled = get_nearest_expiry(name)
            rng           = config.OPTION_CACHE_RANGE[name]

            matches = [
                i for i in instruments
                if i["name"] == name
                and i["instrument_type"] in ("CE", "PE")
                and i["expiry"] == expiry
                and abs(i["strike"] - spot) <= rng
            ]
            for m in matches:
                key = f"{name}_{int(m['strike'])}_{m['instrument_type']}"
                result[key] = {
                    "token":         m["instrument_token"],
                    "tradingsymbol": m["tradingsymbol"],
                    "lot_size":      m["lot_size"],
                }
            print(
                f"[kite_client] {name}: cached {len(matches)} strikes "
                f"around {spot:.0f}, expiry {expiry}"
                + (" [rolled forward]" if rolled else "")
            )

        from src.state import redis_set
        redis_set("kite:option_tokens", json.dumps(result), ex=86400)
        print(f"[kite_client] Total cached: {len(result)} option tokens")

    except Exception as e:
        print(f"[kite_client] cache_option_tokens failed: {e}")


def estimate_atm_delta(instrument_name: str, atm_strike: int,
                       direction: str, step: int) -> float:
    """Central-difference delta from adjacent-strike premiums.
        delta ≈ (P(K∓step) − P(K±step)) / (2·step)
    Reuses the kite:option_tokens cache. Falls back to config.ATM_DELTA
    on any missing leg, illiquid strike, or out-of-band (<0.15 or >0.85) result.
    """
    fallback = config.ATM_DELTA
    try:
        from src.state import redis_get
        raw = redis_get("kite:option_tokens")
        if not raw:
            print("[kite_client] delta: option cache empty — fallback")
            return fallback

        token_map = json.loads(raw)
        opt_type  = "CE" if direction.upper() == "CE" else "PE"
        k_lo, k_hi = int(atm_strike - step), int(atm_strike + step)
        lo = token_map.get(f"{instrument_name}_{k_lo}_{opt_type}")
        hi = token_map.get(f"{instrument_name}_{k_hi}_{opt_type}")
        if not lo or not hi:
            print(f"[kite_client] delta: neighbor strikes uncached "
                  f"({instrument_name} {k_lo}/{k_hi} {opt_type}) — fallback")
            return fallback

        exchange = config.fno_exchange_for(instrument_name)
        lo_key   = f"{exchange}:{lo['tradingsymbol']}"
        hi_key   = f"{exchange}:{hi['tradingsymbol']}"
        ltp      = get_kite().ltp([lo_key, hi_key])
        p_lo = ltp.get(lo_key, {}).get("last_price")
        p_hi = ltp.get(hi_key, {}).get("last_price")
        if not p_lo or not p_hi:
            print("[kite_client] delta: zero/None premium leg — fallback")
            return fallback

        # CE: lower strike richer → positive. PE: higher strike richer.
        delta = (p_lo - p_hi) / (2 * step) if opt_type == "CE" \
                else (p_hi - p_lo) / (2 * step)
        delta = abs(delta)
        if not (0.15 <= delta <= 0.85):
            print(f"[kite_client] delta {delta:.3f} out of band — fallback")
            return fallback

        print(f"[kite_client] {instrument_name} {opt_type} est. delta={delta:.3f} "
              f"(P{k_lo}={p_lo}, P{k_hi}={p_hi})")
        return round(delta, 3)
    except Exception as e:
        print(f"[kite_client] estimate_atm_delta failed: {e} — fallback {fallback}")
        return fallback


def get_atm_option(instrument_name: str, spot_price: float,
                   direction: str, step: int) -> dict:
    """Returns the single ATM option contract with its live LTP.
    Called as the last step before building the alert.
    Returns empty dict on any failure — caller must handle gracefully.
    """
    try:
        from src.state import redis_get
        atm = round(spot_price / step) * step

        raw = redis_get("kite:option_tokens")
        if not raw:
            print("[kite_client] Option token cache empty — did morning-login run?")
            return {}

        token_map = json.loads(raw)
        tk_key    = f"{instrument_name}_{int(atm)}_{direction}"
        info      = token_map.get(tk_key)
        if not info:
            # Cache built at 09:05 IST may no longer cover this strike if the
            # instrument has drifted since morning-login. Rebuild once and
            # retry before giving up — this is the LT_4250_PE fix.
            print(f"[kite_client] ATM token not cached: {tk_key} — refreshing cache")
            cache_option_tokens()
            raw = redis_get("kite:option_tokens")
            token_map = json.loads(raw) if raw else {}
            info = token_map.get(tk_key)
        if not info:
            print(f"[kite_client] ATM token still not cached after refresh: {tk_key}")
            return {}

        exchange        = config.fno_exchange_for(instrument_name)
        opt_key         = f"{exchange}:{info['tradingsymbol']}"
        kite            = get_kite()
        ltp_data        = kite.ltp([opt_key])
        ltp_val         = ltp_data.get(opt_key, {}).get("last_price")
        expiry, rolled  = get_nearest_expiry(instrument_name)

        return {
            "tradingsymbol":  info["tradingsymbol"],
            "strike":         atm,
            "ltp":            round(ltp_val, 2) if ltp_val else None,
            "expiry":         expiry.isoformat(),
            "fetch_time":     datetime.now(IST).strftime("%H:%M:%S IST"),
            "rolled_forward": rolled,
        }

    except Exception as e:
        print(f"[kite_client] get_atm_option({instrument_name}) failed: {e}")
        return {}


_HISTORICAL_RATE_LOCK = threading.Lock()
_HISTORICAL_CALL_TIMES: deque = deque()
_HISTORICAL_MAX_PER_SECOND = 3   # Kite's real cap for the historical-data endpoint


def _throttle_historical_call() -> None:
    """Thread-safe limiter: blocks until fewer than 3 historical_data calls
    have started in the trailing 1-second window, across ALL threads. This
    replaces the old per-thread sleep-based throttle, which only worked
    correctly for a single sequential caller."""
    while True:
        with _HISTORICAL_RATE_LOCK:
            now = time.monotonic()
            while _HISTORICAL_CALL_TIMES and now - _HISTORICAL_CALL_TIMES[0] >= 1.0:
                _HISTORICAL_CALL_TIMES.popleft()
            if len(_HISTORICAL_CALL_TIMES) < _HISTORICAL_MAX_PER_SECOND:
                _HISTORICAL_CALL_TIMES.append(now)
                return
        time.sleep(0.05)


def fetch_ohlcv(instrument_token: int, today_open: datetime) -> pd.DataFrame:
    _throttle_historical_call()

    kite = get_kite()
    # Go back 5 calendar days so RSI(14) and DMI(14) always have prior-session
    # warm-up candles regardless of weekends/holidays. timedelta(days=1) on a
    # Monday goes to Sunday (no trading); timedelta(days=5) always captures at
    # least the previous Friday's full session (~75 candles).
    prior_session_start = today_open - timedelta(days=5)

    data = kite.historical_data(
        instrument_token=instrument_token,
        from_date=prior_session_start,
        to_date=datetime.now(IST),
        interval="5minute",
        continuous=False,
        oi=False,
    )

    df = pd.DataFrame(data)
    if df.empty:
        raise RuntimeError(f"No OHLCV data returned for token {instrument_token}")

    df = df.rename(columns={"date": "timestamp"})
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def fetch_ohlcv_multi(instrument_token: int, interval: str, lookback_days: int) -> pd.DataFrame:
    """Historical OHLCV fetch for an arbitrary Kite interval (e.g. "60minute",
    "15minute") over `lookback_days` calendar days ending now. Shares the same
    rate limiter as fetch_ohlcv(); used by pvwap_signals.py for multi-timeframe
    swing/zone/Fibonacci analysis, which needs windows fetch_ohlcv() (fixed at
    5minute/5-day) cannot provide."""
    _throttle_historical_call()

    kite = get_kite()
    from_date = datetime.now(IST) - timedelta(days=lookback_days)

    data = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_date,
        to_date=datetime.now(IST),
        interval=interval,
        continuous=False,
        oi=False,
    )

    df = pd.DataFrame(data)
    if df.empty:
        raise RuntimeError(f"No {interval} data returned for token {instrument_token}")

    df = df.rename(columns={"date": "timestamp"})
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def get_live_quotes_batch(keys: list[str]) -> dict[str, dict]:
    """
    Fetches live LTP and live session VWAP for MULTIPLE instruments in a
    SINGLE kite.quote() call (Kite's quote endpoint supports up to 500
    instruments per call). `keys` are "EXCHANGE:TRADINGSYMBOL" strings, e.g.
    ["NFO:NIFTY26JUNFUT", "NSE:INFY", "NSE:SBIN"].

    Returns {key: {"ltp": float, "vwap": float}} — only for keys that had
    valid last_price AND average_price in the response. A key missing from
    the result means "skip this instrument this run". Returns {} (not None)
    on a total failure so callers can safely use .get(key) without a
    None-check on the whole dict.
    """
    if not keys:
        return {}
    try:
        kite = get_kite()
        data = kite.quote(keys)
    except Exception as e:
        print(f"[kite_client] get_live_quotes_batch({len(keys)} keys) failed: {e}")
        return {}

    result = {}
    for key in keys:
        q = data.get(key)
        if not q:
            print(f"[kite_client] get_live_quotes_batch: no data for {key}")
            continue
        ltp = q.get("last_price")
        vwap = q.get("average_price")
        if ltp is None or vwap is None:
            print(f"[kite_client] get_live_quotes_batch: missing last_price/average_price for {key}")
            continue
        result[key] = {"ltp": float(ltp), "vwap": float(vwap)}
    return result


if __name__ == "__main__":
    import sys
    if "--cache-instruments" in sys.argv:
        resolve_futures_tokens()
    if "--cache-option-tokens" in sys.argv:
        cache_option_tokens()
    if "--pvwap-premarket" in sys.argv:
        from src import pvwap_signals
        pvwap_signals.run_premarket()
