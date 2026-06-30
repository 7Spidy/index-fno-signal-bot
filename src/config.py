import os

INSTRUMENTS = [
    {"name": "NIFTY",     "strike_step": 50,  "min_risk": 10, "fno_exchange": "NFO"},
    {"name": "BANKNIFTY", "strike_step": 100, "min_risk": 30, "fno_exchange": "NFO"},
    {"name": "SENSEX",    "strike_step": 100, "min_risk": 30, "fno_exchange": "BFO"},
]

# Spot index tradingsymbols for kite.ltp() — format is "NSE:<symbol>".
# Integer instrument tokens (256265, 260105) do NOT work as ltp() keys.
SPOT_TOKENS = {
    "NIFTY":     "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "SENSEX":    "SENSEX",
}

# Exchange segment for spot LTP lookups. NIFTY/BANKNIFTY are on NSE; SENSEX is on BSE.
SPOT_EXCHANGE = {
    "NIFTY":     "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX":    "BSE",
}

# NIFTY: weekly, Tuesday expiry — hardcoded weekday path.
# BANKNIFTY: monthly only — resolved from the live NFO dump.
# SENSEX: has weeklies (currently Thursday) — resolved from the live BFO dump,
#         not hardcoded, so it survives future expiry-day changes.
USE_WEEKLY = {
    "NIFTY":     True,
    "BANKNIFTY": False,
    "SENSEX":    False,
}

# Weekly expiry weekday for the CALENDAR FALLBACK path only (Mon=0 … Sun=6).
# Primary expiry resolution is the live Kite dump (handles holiday shifts);
# this is used solely when the dump is unreachable.
WEEKLY_EXPIRY_WEEKDAY = {
    "NIFTY":  1,   # Tuesday
    "SENSEX": 3,   # Thursday
    # BANKNIFTY omitted — monthly only, falls through to month-end fallback
}

# Instruments using monthly (not weekly) expiry — BANKNIFTY + all stocks.
MONTHLY_EXPIRY_INSTRUMENTS = {"BANKNIFTY"}  # stocks handled via stock_config.STOCKS

# Cutoff time on an instrument's own expiry day — no alerts fired after this,
# even though the normal EVAL_WINDOW_END (14:45) is later. Does not affect
# instruments that are NOT expiring today.
EXPIRY_DAY_CUTOFF = "13:30"

# Strike range (pts from spot) to pre-cache at morning-login.
OPTION_CACHE_RANGE = {
    "NIFTY":     500,
    "BANKNIFTY": 1500,
    "SENSEX":    2000,   # ~2.5% of ~78,000 spot
}

# VWAP proximity filter — part of C2.
# Price must be within this many points of VWAP at signal time.
# Prevents entries where price has already extended far from VWAP,
# replacing the old max-risk candle-width gate.
# Values are index-futures points (not option premium).
VWAP_PROXIMITY_PTS = {
    "NIFTY":     40,
    "BANKNIFTY": 200,
    "SENSEX":    160,
}

# Uniform R:R target for ALL signals regardless of conviction.
# Changed 3.0 → 1.5 (2026-06-10): base target tightened to bank
# lower-health winners sooner. Runner mode (executor, health ≥ 75) still
# lets strong winners trail PAST 1.5R, so this only governs non-runner exits.
# Break-even win rate at 1.5R = 40%; measured NIFTY backtest = ~41% (thin
# margin — monitor per-index win rate on paper before live).
# Original backtest: May 22 – Jun 5, 2026, NIFTY only, 17 signals.
# Conviction label is still surfaced in the alert for context.
TARGET_RR = 1.5

# Delta used to convert spot risk → option premium SL / Target.
# 0.50 is a reliable approximation for liquid ATM index options.
ATM_DELTA = 0.50

# Evaluation window (IST)
EVAL_WINDOW_START = "09:40"
EVAL_WINDOW_END   = "14:45"

# Compatibility alias used by calendar_nse.py and log messages
EVAL_WINDOW_IST = (EVAL_WINDOW_START, EVAL_WINDOW_END)

# RSI candles for slope check
RSI_SLOPE_CANDLES = 3

# Cooldown: minimum candles between same-direction signals
COOLDOWN_CANDLES = 3


def fno_exchange_for(name: str) -> str:
    """Returns the F&O exchange segment for an underlying (NFO or BFO)."""
    for inst in INSTRUMENTS:
        if inst["name"] == name:
            return inst.get("fno_exchange", "NFO")
    return "NFO"


def as_dict() -> dict:
    return {
        "MOMENTUM_RULE":             "close_gt_prev_close",
        "RSI_SLOPE_LOOKBACK":        RSI_SLOPE_CANDLES,
        "VWAP_CROSS_WINDOW_CANDLES": 6,
        "DI_THRESHOLD":              25,
        "REQUIRE_DI_DOMINANCE":      True,
        "DI_TREND_CHECK":            True,
        "USE_ADX_FILTER":            False,
        "ADX_MIN":                   20,
        "COOLDOWN_CANDLES":          COOLDOWN_CANDLES,
        "SESSION_START_IST":         "09:15",
        "EVAL_WINDOW_IST":           EVAL_WINDOW_IST,
        "INSTRUMENTS":               INSTRUMENTS,
        "TARGET_RR":                 TARGET_RR,
        "ATM_DELTA":                 ATM_DELTA,
        "VWAP_PROXIMITY_PTS":        VWAP_PROXIMITY_PTS,
    }
