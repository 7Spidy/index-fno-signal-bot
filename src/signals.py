"""CE/PE signal evaluation — 4 conditions each, per spec §6."""
import pandas as pd
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def evaluate(df: pd.DataFrame, vwap: pd.Series, rsi: pd.Series,
             pdi: pd.Series, ndi: pd.Series, cfg: dict) -> dict:
    """
    Evaluates CE and PE conditions on the latest fully-closed candle.

    c[0] = df.iloc[-2] (latest closed candle)
    c[1] = df.iloc[-3] (prior candle)
    """
    strike_step = cfg.get("strike_step", 50)

    if len(df) < 4:
        return _empty_result(df, vwap, rsi, pdi, ndi, strike_step)

    if rsi.dropna().shape[0] < 15 or pdi.dropna().shape[0] < 15 or ndi.dropna().shape[0] < 15:
        print("[signals] Insufficient non-null indicator values — skipping evaluation")
        return _empty_result(df, vwap, rsi, pdi, ndi, strike_step)

    idx0 = len(df) - 2  # latest closed candle index
    idx1 = len(df) - 3  # prior candle index

    c0 = df.iloc[idx0]
    c1 = df.iloc[idx1]

    v0 = vwap.iloc[idx0]
    r0 = rsi.iloc[idx0]
    p0 = pdi.iloc[idx0]
    n0 = ndi.iloc[idx0]

    momentum_rule     = cfg.get("MOMENTUM_RULE", "close_gt_prev_close")
    rsi_lookback      = cfg.get("RSI_SLOPE_LOOKBACK", 3)
    vwap_window       = cfg.get("VWAP_CROSS_WINDOW_CANDLES", 6)
    di_threshold      = cfg.get("DI_THRESHOLD", 25)
    require_dominance = cfg.get("REQUIRE_DI_DOMINANCE", True)
    di_trend_check    = cfg.get("DI_TREND_CHECK", True)

    # C1 — Momentum
    if momentum_rule == "close_gt_prev_close":
        ce_c1 = bool(c0["close"] > c1["close"])
        pe_c1 = bool(c0["close"] < c1["close"])
    else:  # open_gt_prev_close
        ce_c1 = bool(c0["open"] > c1["close"])
        pe_c1 = bool(c0["open"] < c1["close"])

    # C2 — VWAP cross within last vwap_window candles
    ce_c2 = False
    pe_c2 = False
    if pd.notna(v0):
        currently_above = c0["close"] > v0
        currently_below = c0["close"] < v0
        for k in range(1, vwap_window + 1):
            past_idx = idx0 - k
            if past_idx < 0:
                break
            past_close = df.iloc[past_idx]["close"]
            past_vwap = vwap.iloc[past_idx]
            if pd.isna(past_vwap):
                continue
            if currently_above and past_close <= past_vwap:
                ce_c2 = True
                break
            if currently_below and past_close >= past_vwap:
                pe_c2 = True
                break

    # C3 — RSI slope over rsi_lookback candles
    ce_c3 = False
    pe_c3 = False
    if idx0 >= rsi_lookback:
        rsi_vals = [rsi.iloc[idx0 - i] for i in range(rsi_lookback)]
        if all(pd.notna(v) for v in rsi_vals):
            ce_c3 = all(rsi_vals[i] > rsi_vals[i + 1] for i in range(rsi_lookback - 1))
            pe_c3 = all(rsi_vals[i] < rsi_vals[i + 1] for i in range(rsi_lookback - 1))

    # C4 — DI threshold, dominance, and (optionally) the dominant DI rising
    ce_c4 = False
    pe_c4 = False
    if pd.notna(p0) and pd.notna(n0):
        pdi_now, ndi_now = p0, n0
        ce_c4 = bool(pdi_now > di_threshold and (pdi_now > ndi_now if require_dominance else True))
        pe_c4 = bool(ndi_now > di_threshold and (ndi_now > pdi_now if require_dominance else True))

        if di_trend_check:
            pdi_rising = False
            ndi_rising = False
            idx2 = idx0 - 2   # two candles prior to the latest closed candle
            if (pdi.dropna().shape[0] >= 3 and ndi.dropna().shape[0] >= 3
                    and idx2 >= 0 and idx1 >= 0):
                pdi_p1 = pdi.iloc[idx1]   # one candle back
                pdi_p2 = pdi.iloc[idx2]   # two candles back
                ndi_p1 = ndi.iloc[idx1]
                ndi_p2 = ndi.iloc[idx2]
                if pd.notna(pdi_p1) and pd.notna(pdi_p2):
                    pdi_rising = bool(pdi_now > pdi_p1 > pdi_p2)
                if pd.notna(ndi_p1) and pd.notna(ndi_p2):
                    ndi_rising = bool(ndi_now > ndi_p1 > ndi_p2)
            ce_c4 = ce_c4 and pdi_rising
            pe_c4 = pe_c4 and ndi_rising

    ce_signal = ce_c1 and ce_c2 and ce_c3 and ce_c4
    pe_signal = pe_c1 and pe_c2 and pe_c3 and pe_c4

    # Guard — both firing simultaneously is theoretically impossible
    if ce_signal and pe_signal:
        print("[signals] WARNING: Both CE and PE fired simultaneously — suppressing both")
        ce_signal = False
        pe_signal = False

    price = float(c0["close"])
    atm_strike = round(price / strike_step) * strike_step

    return {
        "ce": {"c1": ce_c1, "c2": ce_c2, "c3": ce_c3, "c4": ce_c4, "signal": ce_signal},
        "pe": {"c1": pe_c1, "c2": pe_c2, "c3": pe_c3, "c4": pe_c4, "signal": pe_signal},
        "futures_price":    round(price, 2),
        "candle_high":      round(float(c0["high"]), 2),
        "candle_low":       round(float(c0["low"]),  2),
        "prev_candle_high": round(float(c1["high"]), 2),
        "prev_candle_low":  round(float(c1["low"]),  2),
        "candle_time":      _fmt_candle_time(c0["timestamp"]),
        "vwap":          float(v0) if pd.notna(v0) else None,
        "rsi":           float(r0) if pd.notna(r0) else None,
        "pdi":           float(p0) if pd.notna(p0) else None,
        "ndi":           float(n0) if pd.notna(n0) else None,
        "atm_strike":    int(atm_strike),
    }


def _empty_result(df, vwap, rsi, pdi, ndi, strike_step):
    empty_side = {"c1": False, "c2": False, "c3": False, "c4": False, "signal": False}
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
        "atm_strike":    None,
    }


def _fmt_candle_time(ts) -> str:
    try:
        if hasattr(ts, "astimezone"):
            return ts.astimezone(IST).strftime("%H:%M IST")
        return str(ts)
    except Exception:
        return str(ts)
