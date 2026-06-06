import os

INSTRUMENTS = [
    {"name": "NIFTY",     "strike_step": 50,  "min_risk": 10},
    {"name": "BANKNIFTY", "strike_step": 100, "min_risk": 30},
]

# Spot index tokens (NSE segment — verified from Kite API)
SPOT_TOKENS = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
}

# NIFTY: weekly options exist, expire every TUESDAY.
# BANKNIFTY: weekly options discontinued — use MONTHLY expiry only.
USE_WEEKLY = {
    "NIFTY":     True,
    "BANKNIFTY": False,
}

# Strike range (pts from spot) to pre-cache at morning-login.
OPTION_CACHE_RANGE = {
    "NIFTY":     500,
    "BANKNIFTY": 1500,
}

# Max risk filter.
# Risk = candle_high - futures_close  (PE)
#      = futures_close - candle_low   (CE)
# Wide-candle signals mean the entry is after the move;
# SL is far; R:R collapses. Skip these cleanly with a log line.
MAX_RISK_POINTS = {
    "NIFTY":     25,   # 25 pts × lot 65  ≈ ₹1,625 max per lot
    "BANKNIFTY": 60,   # 60 pts × lot 30  ≈ ₹1,800 max per lot
}

# Uniform R:R target for ALL signals regardless of conviction.
# Backtested May 22 – Jun 5, 2026 (17 signals, 41% win rate):
# win rate is flat across R:R levels; R:R 3.0 gives highest P&L.
# Conviction label is still surfaced in the alert for context.
TARGET_RR = 3.0

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


def as_dict() -> dict:
    return {
        "MOMENTUM_RULE":             "close_gt_prev_close",
        "RSI_SLOPE_LOOKBACK":        RSI_SLOPE_CANDLES,
        "VWAP_CROSS_WINDOW_CANDLES": 6,
        "DI_THRESHOLD":              25,
        "REQUIRE_DI_DOMINANCE":      True,
        "USE_ADX_FILTER":            False,
        "ADX_MIN":                   20,
        "COOLDOWN_CANDLES":          COOLDOWN_CANDLES,
        "SESSION_START_IST":         "09:15",
        "EVAL_WINDOW_IST":           EVAL_WINDOW_IST,
        "INSTRUMENTS":               INSTRUMENTS,
        "TARGET_RR":                 TARGET_RR,
        "ATM_DELTA":                 ATM_DELTA,
    }
