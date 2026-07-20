"""Morning sector-relative-strength computation. Run once daily by
morning-login.yml. Writes a static Redis snapshot for the day — never
re-evaluated intraday. Failure is silent: missing key = no conviction tags
fired that day (fails open)."""

import json
from datetime import date, timedelta

from src import sector_config as cfg
from src.kite_client import get_kite
from src.state import redis_set


def _three_session_return_pct(kite, tradingsymbol: str) -> float | None:
    """Fetch last LOOKBACK_SESSIONS+1 daily candles via kite.historical_data()
    for exchange NSE, return close-to-close % over the window. Returns None
    on any failure (missing symbol, insufficient candles, API error) —
    caller must treat None as 'exclude this sector, do not tag'."""
    try:
        today = date.today()
        # Need LOOKBACK_SESSIONS+1 closes to compute LOOKBACK_SESSIONS returns.
        # Fetch a wider window (10 calendar days) to handle weekends/holidays.
        from_date = today - timedelta(days=10)
        candles = kite.historical_data(
            instrument_token=_resolve_token(kite, tradingsymbol),
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=today.strftime("%Y-%m-%d"),
            interval="day",
            continuous=False,
        )
        if not candles or len(candles) < cfg.LOOKBACK_SESSIONS + 1:
            print(f"[sector_perf] {tradingsymbol}: insufficient candles "
                  f"({len(candles) if candles else 0}) — skipping")
            return None
        # Use last LOOKBACK_SESSIONS+1 candles
        relevant = candles[-(cfg.LOOKBACK_SESSIONS + 1):]
        start_close = float(relevant[0]["close"])
        end_close   = float(relevant[-1]["close"])
        if start_close == 0:
            return None
        return (end_close - start_close) / start_close * 100
    except Exception as e:
        print(f"[sector_perf] {tradingsymbol}: error fetching return — {e}")
        return None


def _resolve_token(kite, tradingsymbol: str) -> int:
    """Resolve NSE instrument token for a tradingsymbol (index or equity)."""
    instruments = kite.instruments("NSE")
    for i in instruments:
        if i["tradingsymbol"] == tradingsymbol:
            return i["instrument_token"]
    raise ValueError(f"Instrument not found in NSE dump: {tradingsymbol!r}")


def compute_sector_performance() -> dict:
    """Returns {sector_key: {"spread_pct": float, "tag": "OUT"|"UNDER"|"NEUTRAL"}}
    for every sector with a successfully resolved spread. Sectors that fail
    to resolve are omitted entirely (not written as NEUTRAL — omitted)."""
    kite = get_kite()
    nifty_ret = _three_session_return_pct(kite, cfg.NIFTY50_SYMBOL)
    if nifty_ret is None:
        print("[sector_perf] Could not compute NIFTY 50 return — aborting sector performance")
        return {}

    result = {}
    for sector_key, index_symbol in cfg.SECTOR_INDEX_SYMBOL.items():
        sector_ret = _three_session_return_pct(kite, index_symbol)
        if sector_ret is None:
            print(f"[sector_perf] {sector_key} ({index_symbol}): unresolved — omitted")
            continue

        spread = sector_ret - nifty_ret
        if spread > cfg.SPREAD_THRESHOLD_PCT:
            tag = "OUT"
        elif spread < -cfg.SPREAD_THRESHOLD_PCT:
            tag = "UNDER"
        else:
            tag = "NEUTRAL"

        result[sector_key] = {"spread_pct": round(spread, 4), "tag": tag}
        print(f"[sector_perf] {sector_key}: sector={sector_ret:.2f}% nifty={nifty_ret:.2f}% "
              f"spread={spread:.2f}% → {tag}")

    return result


def cache_sector_performance() -> None:
    """Entry point. Computes and writes
    stock:sector_perf:{YYYY-MM-DD} to Redis (TTL ~16h, survives the trading
    day). Never raises past this function — always exits 0."""
    try:
        perf = compute_sector_performance()
        if not perf:
            print("[sector_perf] No sector data to cache — skipping Redis write")
            return
        key = f"stock:sector_perf:{date.today().isoformat()}"
        redis_set(key, json.dumps(perf), ex=57600)  # 16 hours
        print(f"[sector_perf] Cached {len(perf)} sectors to Redis key {key!r}")
    except Exception as e:
        print(f"[sector_perf] cache_sector_performance failed (non-fatal): {e}")


if __name__ == "__main__":
    if "--cache-sector-performance" in __import__("sys").argv:
        cache_sector_performance()
