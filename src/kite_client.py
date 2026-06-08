"""Kite Connect client: session bootstrap, instrument resolver, OHLCV fetcher."""
import json
import os
import sys
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


def get_kite() -> KiteConnect:
    api_key = os.environ["KITE_API_KEY"]
    kite = KiteConnect(api_key=api_key)
    token = state.redis_get("kite:access_token")
    if not token:
        raise RuntimeError("No access token in Redis. Run morning-login.yml first.")
    kite.set_access_token(token)
    return kite


def resolve_futures_tokens(kite: KiteConnect | None = None) -> dict:
    if kite is None:
        kite = get_kite()

    print("[kite] Fetching NFO instrument list...")
    instruments = kite.instruments("NFO")

    result = {}
    for inst in config.INSTRUMENTS:
        underlying = inst["name"]
        futs = [i for i in instruments
                if i["name"] == underlying
                and i["instrument_type"] == "FUT"
                and i["expiry"] >= date.today()]
        if not futs:
            print(f"[kite] WARNING: No futures found for {underlying}")
            continue
        nearest = min(futs, key=lambda x: x["expiry"])
        result[underlying] = {
            "token": nearest["instrument_token"],
            "expiry": nearest["expiry"].isoformat(),
            "tradingsymbol": nearest["tradingsymbol"],
            "strike_step": inst["strike_step"],
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
        kite = get_kite()
        key  = f"NSE:{token}"
        data = kite.ltp([key])
        return data[key]["last_price"]
    except Exception as e:
        print(f"[kite_client] get_spot_ltp({instrument_name}) failed: {e}")
        return None


def get_nearest_expiry(instrument_name: str) -> date:
    """Returns the nearest valid option expiry.

    NIFTY (USE_WEEKLY=True): next Tuesday; rolls if today IS Tuesday and >= 15:30 IST.
    BANKNIFTY (USE_WEEKLY=False): nearest monthly expiry from Kite NFO instruments.
    """
    try:
        now_ist = datetime.now(IST)
        today   = now_ist.date()

        if config.USE_WEEKLY.get(instrument_name, False):
            days_ahead = (1 - today.weekday()) % 7
            if days_ahead == 0:
                if now_ist.hour >= 15 and now_ist.minute >= 30:
                    days_ahead = 7
            return today + timedelta(days=days_ahead)

        else:
            from src.state import redis_get, redis_set
            cache_key = f"kite:expiry:{instrument_name}"
            cached = redis_get(cache_key)
            if cached:
                exp_date = date.fromisoformat(cached)
                if exp_date >= today:
                    return exp_date

            kite = get_kite()
            instruments = kite.instruments("NFO")
            monthly = [
                i for i in instruments
                if i["name"] == instrument_name
                and i["instrument_type"] in ("CE", "PE")
                and i.get("expiry_type") == "monthly"
                and i["expiry"] >= today
            ]
            if not monthly:
                raise ValueError(f"No monthly expiry found for {instrument_name}")
            nearest  = min(monthly, key=lambda x: x["expiry"])
            exp_date = nearest["expiry"]
            redis_set(cache_key, exp_date.isoformat(), ex=21600)
            print(f"[kite_client] {instrument_name} monthly expiry: {exp_date}")
            return exp_date

    except Exception as e:
        print(f"[kite_client] get_nearest_expiry({instrument_name}) failed: {e}")
        import calendar as _cal
        today = date.today()
        last  = _cal.monthrange(today.year, today.month)[1]
        return date(today.year, today.month, last)


def cache_option_tokens() -> None:
    """Called once from morning-login. Fetches NFO instruments and stores a compact
    token map in Redis (key 'kite:option_tokens', TTL 24 hours)."""
    try:
        kite        = get_kite()
        instruments = kite.instruments("NFO")
        result      = {}

        for inst_cfg in config.INSTRUMENTS:
            name   = inst_cfg["name"]
            step   = inst_cfg["strike_step"]
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

        nfo_key  = f"NFO:{info['token']}"
        kite     = get_kite()
        ltp_data = kite.ltp([nfo_key])
        ltp_val  = ltp_data.get(nfo_key, {}).get("last_price")
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


def fetch_ohlcv(instrument_token: int, today_open: datetime) -> pd.DataFrame:
    kite = get_kite()
    # Fetch from yesterday's session open so RSI(14) and DMI(14) have ~75
    # warm-up candles. today_open - timedelta(hours=3) landed at 06:15 IST
    # (before market open), giving zero prior-session data.
    prior_session_start = today_open - timedelta(days=1)

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


if __name__ == "__main__":
    import sys
    if "--cache-instruments" in sys.argv:
        resolve_futures_tokens()
    if "--cache-option-tokens" in sys.argv:
        cache_option_tokens()
