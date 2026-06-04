import os

INSTRUMENTS = [
    {"name": "NIFTY",      "strike_step": 50},
    {"name": "BANKNIFTY",  "strike_step": 100},
    {"name": "FINNIFTY",   "strike_step": 50},
    {"name": "MIDCPNIFTY", "strike_step": 25},
]

MOMENTUM_RULE = "close_gt_prev_close"
RSI_SLOPE_LOOKBACK = 3
VWAP_CROSS_WINDOW_CANDLES = 6
DI_THRESHOLD = 25
REQUIRE_DI_DOMINANCE = True
DI_TREND_CHECK = True   # require the dominant DI to be rising
USE_ADX_FILTER = False
ADX_MIN = 20
COOLDOWN_CANDLES = 3
SESSION_START_IST = "09:15"
EVAL_WINDOW_IST = ("09:40", "14:45")

# Stop Loss / Target / Conviction (futures price levels)
MIN_RISK = 10                # min risk in points, avoids absurdly tight targets
CONVICTION_STRONG_SPREAD = 18   # DI spread >= this → Strong conviction (1:3.0)
CONVICTION_MODERATE_SPREAD = 10  # DI spread >= this → Moderate conviction (1:2.0)


def as_dict() -> dict:
    return {
        "MOMENTUM_RULE": MOMENTUM_RULE,
        "RSI_SLOPE_LOOKBACK": RSI_SLOPE_LOOKBACK,
        "VWAP_CROSS_WINDOW_CANDLES": VWAP_CROSS_WINDOW_CANDLES,
        "DI_THRESHOLD": DI_THRESHOLD,
        "REQUIRE_DI_DOMINANCE": REQUIRE_DI_DOMINANCE,
        "DI_TREND_CHECK": DI_TREND_CHECK,
        "USE_ADX_FILTER": USE_ADX_FILTER,
        "ADX_MIN": ADX_MIN,
        "COOLDOWN_CANDLES": COOLDOWN_CANDLES,
        "SESSION_START_IST": SESSION_START_IST,
        "EVAL_WINDOW_IST": EVAL_WINDOW_IST,
        "MIN_RISK": MIN_RISK,
        "CONVICTION_STRONG_SPREAD": CONVICTION_STRONG_SPREAD,
        "CONVICTION_MODERATE_SPREAD": CONVICTION_MODERATE_SPREAD,
        "INSTRUMENTS": INSTRUMENTS,
    }
