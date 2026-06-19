"""Kite Connect client: session bootstrap, instrument resolver, OHLCV fetcher."""
import json
import os
import sys
import time
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


def get_kite() -> KiteConnect:
    """Returns a cached KiteConnect client for this process. The access token
    is read from Redis once per process lifetime (one GHA run = one process),
    not on every call site. A failed lookup never poisons the cache — only a
    successful token fetch is stored."""
    global _KITE_SINGLETON
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


def get_nearest_expiry(instrument_name: str) -> date:
    """Nearest valid option expiry. Resolved from the LIVE instrument dump for
    all instruments — survives NSE holiday shifts and expiry-day rule changes.
    Calendar-aware fallback only when the dump is unreachable.
    """
    from datetime import time as dtime
    now_ist     = datetime.now(IST)
    today       = now_ist.date()
    after_close = now_ist.time() >= dtime(15, 30)   # fixed: true for all of 15:30→23:59

    from src.state import redis_get, redis_set
    cache_key = f"kite:expiry:{instrument_name}"
    cached = redis_get(cache_key)
    if cached:
        try:
            exp = date.fromisoformat(cached)
            if exp > today or (exp == today and not after_close):
                return exp
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
        if after_close:                       # post-close on expiry day → roll forward
            opts = [i for i in opts if i["expiry"] > today] or opts
        if not opts:
            raise ValueError(f"no options for {instrument_name} on {exchange}")
        exp = min(opts, key=lambda x: x["expiry"])["expiry"]
        redis_set(cache_key, exp.isoformat(), ex=21600)
        print(f"[kite_client] {instrument_name} nearest expiry (live): {exp}")
        return exp
    except Exception as e:
        print(f"[kite_client] live expiry resolve failed for {instrument_name}: {e}")

    # ── Calendar-aware fallback (no Kite): weekly instruments ──
    weekday = config.WEEKLY_EXPIRY_WEEKDAY.get(instrument_name)
    if weekday is not None:
        from src import calendar_nse
        days_ahead = (weekday - today.weekday()) % 7
        if days_ahead == 0 and after_close:
            days_ahead = 7
        cand = today + timedelta(days=days_ahead)
        while not calendar_nse.is_trading_day(cand):   # holiday → previous trading day
            cand -= timedelta(days=1)
        print(f"[kite_client] {instrument_name} nearest expiry (calendar fallback): {cand}")
        return cand

    # ── Monthly fallback ──
    import calendar as _cal
    last = _cal.monthrange(today.year, today.month)[1]
    return date(today.year, today.month, last)


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
            spot   = get_spot_ltp(name) or 0
            expiry = get_nearest_expiry(name)
            rng    = config.OPTION_CACHE_RANGE[name]

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
            print(f"[kite_client] ATM token not cached: {tk_key}")
            return {}

        exchange = config.fno_exchange_for(instrument_name)
        opt_key  = f"{exchange}:{info['tradingsymbol']}"
        kite     = get_kite()
        ltp_data = kite.ltp([opt_key])
        ltp_val  = ltp_data.get(opt_key, {}).get("last_price")
        expiry   = get_nearest_expiry(instrument_name)

        return {
            "tradingsymbol": info["tradingsymbol"],
            "strike":        atm,
            "ltp":           round(ltp_val, 2) if ltp_val else None,
            "expiry":        expiry.isoformat(),
            "fetch_time":    datetime.now(IST).strftime("%H:%M:%S IST"),
        }

    except Exception as e:
        print(f"[kite_client] get_atm_option({instrument_name}) failed: {e}")
        return {}


_LAST_HISTORICAL_CALL = 0.0
_HISTORICAL_MIN_INTERVAL = 0.35  # seconds; keeps us comfortably under Kite's 3 req/sec cap


def fetch_ohlcv(instrument_token: int, today_open: datetime) -> pd.DataFrame:
    global _LAST_HISTORICAL_CALL
    elapsed = time.monotonic() - _LAST_HISTORICAL_CALL
    if elapsed < _HISTORICAL_MIN_INTERVAL:
        time.sleep(_HISTORICAL_MIN_INTERVAL - elapsed)
    _LAST_HISTORICAL_CALL = time.monotonic()

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


def get_live_quote(key: str) -> dict | None:
    """
    Fetches the live LTP and live session VWAP for one instrument via a single
    kite.quote() call. `key` is "EXCHANGE:TRADINGSYMBOL", e.g.
    "NFO:NIFTY26JUNFUT" (index futures) or "NSE:INFY" (stock spot).

    VWAP is read from Kite's own 'average_price' field — see spec §2.1.

    Returns {"ltp": float, "vwap": float}, or None on any failure. Callers must
    treat None as "skip this instrument this run" — never crash the loop.
    """
    try:
        kite = get_kite()
        data = kite.quote([key])
        q = data.get(key)
        if not q:
            print(f"[kite_client] get_live_quote({key}): no data in response")
            return None
        ltp  = q.get("last_price")
        vwap = q.get("average_price")
        if ltp is None or vwap is None:
            print(f"[kite_client] get_live_quote({key}): missing last_price/average_price")
            return None
        return {"ltp": float(ltp), "vwap": float(vwap)}
    except Exception as e:
        print(f"[kite_client] get_live_quote({key}) failed: {e}")
        return None


def get_live_quotes_batch(keys: list[str]) -> dict[str, dict]:
    """
    Fetches live LTP and live session VWAP for MULTIPLE instruments in a
    SINGLE kite.quote() call (Kite's quote endpoint supports up to 500
    instruments per call). `keys` are "EXCHANGE:TRADINGSYMBOL" strings, e.g.
    ["NFO:NIFTY26JUNFUT", "NSE:INFY", "NSE:SBIN"].

    Returns {key: {"ltp": float, "vwap": float}} — only for keys that had
    valid last_price AND average_price in the response. A key missing from
    the result means "skip this instrument this run", same contract as the
    old per-instrument get_live_quote(). Returns {} (not None) on a total
    failure so callers can safely use .get(key) without a None-check on the
    whole dict.
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
