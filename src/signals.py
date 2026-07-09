"""CE/PE signal evaluation — 4 conditions each, per spec §6 (live evaluation)."""
import pandas as pd
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def evaluate(df: pd.DataFrame, vwap: pd.Series, rsi: pd.Series,
             pdi: pd.Series, ndi: pd.Series, cfg: dict,
             live_ltp: float, live_vwap: float,
             live_rsi: float, live_pdi: float, live_ndi: float,
             live_supertrend_dir: bool | None = None) -> dict:
    """
    Evaluates CE and PE conditions against live quote/indicator values
    (fetched/recomputed by the caller this run — see kite_client.get_live_quote
    and indicators.with_live_bar) and the two most recently closed candles.

    P0 = df.iloc[-2] (latest closed candle)
    P1 = df.iloc[-3] (candle before that)
    """
    strike_step = cfg.get("strike_step", 50)

    if len(df) < 4:
        return _empty_result(df, vwap, rsi, pdi, ndi, strike_step)

    if rsi.dropna().shape[0] < 15 or pdi.dropna().shape[0] < 15 or ndi.dropna().shape[0] < 15:
        print("[signals] Insufficient non-null indicator values — skipping evaluation")
        return _empty_result(df, vwap, rsi, pdi, ndi, strike_step)

    if any(v is None or pd.isna(v) for v in (live_ltp, live_vwap, live_rsi, live_pdi, live_ndi)):
        print("[signals] Missing live quote/indicator values — skipping evaluation")
        return _empty_result(df, vwap, rsi, pdi, ndi, strike_step)

    idx0 = len(df) - 2  # P0 — latest closed candle index
    idx1 = len(df) - 3  # P1 — candle before that

    p0 = df.iloc[idx0]
    p1 = df.iloc[idx1]

    v0 = vwap.iloc[idx0]
    r0, r1 = rsi.iloc[idx0], rsi.iloc[idx1]
    pdi0, pdi1 = pdi.iloc[idx0], pdi.iloc[idx1]
    ndi0, ndi1 = ndi.iloc[idx0], ndi.iloc[idx1]

    di_threshold      = cfg.get("DI_THRESHOLD", 25)
    require_dominance = cfg.get("REQUIRE_DI_DOMINANCE", True)
    di_trend_check    = cfg.get("DI_TREND_CHECK", True)

    # C1 — Momentum: live price vs. P0's close
    ce_c1 = bool(live_ltp > p0["close"])
    pe_c1 = bool(live_ltp < p0["close"])

    # C2 — VWAP position (live) + P0 dipped/spiked through VWAP at some point
    ce_c2 = False
    pe_c2 = False
    if pd.notna(v0):
        ce_c2 = bool(live_ltp > live_vwap and p0["low"]  <= v0)
        pe_c2 = bool(live_ltp < live_vwap and p0["high"] >= v0)

    # C3 — RSI direction: live > P0 > P1 (or reverse), no threshold
    ce_c3 = False
    pe_c3 = False
    if pd.notna(r0) and pd.notna(r1):
        ce_c3 = bool(live_rsi > r0 > r1)
        pe_c3 = bool(live_rsi < r0 < r1)

    # C4 — DI threshold, dominance, and the dominant DI rising (live > P0 > P1)
    ce_c4 = False
    pe_c4 = False
    if pd.notna(pdi0) and pd.notna(ndi0):
        ce_c4 = bool(live_pdi > di_threshold and (live_pdi > live_ndi if require_dominance else True))
        pe_c4 = bool(live_ndi > di_threshold and (live_ndi > live_pdi if require_dominance else True))

        if di_trend_check:
            pdi_rising = False
            ndi_rising = False
            if pd.notna(pdi1) and pd.notna(ndi1):
                pdi_rising = bool(live_pdi > pdi0 > pdi1)
                ndi_rising = bool(live_ndi > ndi0 > ndi1)
            ce_c4 = ce_c4 and pdi_rising
            pe_c4 = pe_c4 and ndi_rising

    # C5 — Supertrend(10,5) direction: soft/informational only, never gates
    # ce_signal/pe_signal (see the AND-chain immediately below).
    ce_c5 = bool(live_supertrend_dir is True)
    pe_c5 = bool(live_supertrend_dir is False)

    ce_signal = ce_c1 and ce_c2 and ce_c3 and ce_c4
    pe_signal = pe_c1 and pe_c2 and pe_c3 and pe_c4

    # Guard — both firing simultaneously is theoretically impossible
    if ce_signal and pe_signal:
        print("[signals] WARNING: Both CE and PE fired simultaneously — suppressing both")
        ce_signal = False
        pe_signal = False

    price = float(p0["close"])
    atm_strike = round(price / strike_step) * strike_step

    return {
        "ce": {"c1": ce_c1, "c2": ce_c2, "c3": ce_c3, "c4": ce_c4, "c5": ce_c5, "signal": ce_signal},
        "pe": {"c1": pe_c1, "c2": pe_c2, "c3": pe_c3, "c4": pe_c4, "c5": pe_c5, "signal": pe_signal},
        "futures_price":    round(price, 2),
        "candle_high":      round(float(p0["high"]), 2),
        "candle_low":       round(float(p0["low"]),  2),
        "prev_candle_high": round(float(p1["high"]), 2),
        "prev_candle_low":  round(float(p1["low"]),  2),
        "candle_time":      _fmt_candle_time(p0["timestamp"]),
        "vwap":          float(v0) if pd.notna(v0) else None,
        "rsi":           float(r0) if pd.notna(r0) else None,
        "pdi":           float(pdi0) if pd.notna(pdi0) else None,
        "ndi":           float(ndi0) if pd.notna(ndi0) else None,
        "live_price":    float(live_ltp),
        "live_vwap":     float(live_vwap),
        "live_rsi":      float(live_rsi),
        "live_pdi":      float(live_pdi),
        "live_ndi":      float(live_ndi),
        "atm_strike":    int(atm_strike),
    }


def _empty_result(df, vwap, rsi, pdi, ndi, strike_step):
    empty_side = {"c1": False, "c2": False, "c3": False, "c4": False, "c5": False, "signal": False}
    return {
        "ce":            empty_side,
        "pe":            dict(empty_side),
        "futures_price":    None,
        "candle_high":      None,
        "candle_low":       None,
        "prev_candle_high": None,
        "prev_candle_low":  None,
        "candle_time":      None,
        "vwap":          None,
        "rsi":           None,
        "pdi":           None,
        "ndi":           None,
        "live_price":    None,
        "live_vwap":     None,
        "live_rsi":      None,
        "live_pdi":      None,
        "live_ndi":      None,
        "atm_strike":    None,
    }


def _fmt_candle_time(ts) -> str:
    try:
        if hasattr(ts, "astimezone"):
            return ts.astimezone(IST).strftime("%H:%M IST")
        return str(ts)
    except Exception:
        return str(ts)
