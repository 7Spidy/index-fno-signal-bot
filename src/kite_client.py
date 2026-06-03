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


def fetch_ohlcv(instrument_token: int, today_open: datetime) -> pd.DataFrame:
    kite = get_kite()
    # Go back 2 calendar days at 09:00 to capture the full prior session for Wilder warm-up
    from_date = (today_open - timedelta(days=2)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )

    data = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_date,
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-instruments", action="store_true")
    args = parser.parse_args()

    if args.cache_instruments:
        kite = get_kite()
        tokens = resolve_futures_tokens(kite)
        print(json.dumps(tokens, indent=2))
    else:
        kite = get_kite()
        today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
        tokens = resolve_futures_tokens(kite)
        nifty_token = tokens["NIFTY"]["token"]
        df = fetch_ohlcv(nifty_token, today_open)
        print(f"NIFTY DataFrame shape: {df.shape}")
        print(df.tail())
