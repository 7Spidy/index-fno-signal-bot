"""
stock_config.py — Configuration for the 14-stock F&O signal bot.

All stocks use monthly expiry only (no weekly — SEBI post-Nov 2024).
OHLCV source: NSE equity tokens (real volume, accurate VWAP).
Options: NFO segment, monthly chain.

Risk gate: VWAP proximity only — consistent with the index bot design.
No candle-width (max_risk_pts) gate. If a signal fires and the candle
is too wide for the capital rule, the alert surfaces it and you decide.

lot_size is metadata only — surfaced in the Discord alert so you can
judge capital implication. Not used in any gate or condition logic.

Verify lot sizes quarterly from kite.instruments("NFO") — NSE revises them.
"""

# ── Option delta approximation (SL/target translation) ──────────────────────
# Moneyness-bucket lookup, NOT a real Greek. Replaces the old flat 0.50
# delta assumption with a deterministic table based on % distance between
# live spot and the resolved ATM strike. No IV back-solve, no Black-Scholes —
# intentionally kept simple and fast since this runs inline on every signal
# fire. moneyness_pct is signed: positive = ITM, negative = OTM (computed
# per-direction in stock_main._compute_moneyness_pct).
#
# Each tuple is (upper_bound_pct, delta). Walked ascending; the first bucket
# whose upper_bound_pct >= moneyness_pct is used. Symmetric around ATM.
# INVARIANT: must remain ascending and end with (float("inf"), ...) — the
# inf bucket guarantees every moneyness value finds a match.
DELTA_MONEYNESS_BUCKETS = [
    (-2.0, 0.35),          # deep OTM
    (-1.0, 0.40),          # OTM
    (-0.3, 0.45),          # near OTM
    (0.3,  0.50),          # ATM
    (1.0,  0.55),          # near ITM
    (2.0,  0.60),          # ITM
    (float("inf"), 0.65),  # deep ITM
]

# Fallback delta used when moneyness cannot be computed (missing strike,
# missing spot, division error, etc.). Matches the old flat-delta behavior.
DELTA_FALLBACK = 0.50

STOCKS = [
    {
        "name":          "RELIANCE",
        "equity_symbol": "RELIANCE",
        "sector":        "Energy/Conglomerate",
        "strike_step":   50,
        "lot_size":      250,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "ICICIBANK",
        "equity_symbol": "ICICIBANK",
        "sector":        "Private Banking",
        "strike_step":   20,
        "lot_size":      700,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "INFY",
        "equity_symbol": "INFY",
        "sector":        "IT",
        "strike_step":   20,
        "lot_size":      400,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "BAJFINANCE",
        "equity_symbol": "BAJFINANCE",
        "sector":        "NBFC",
        "strike_step":   100,
        "lot_size":      125,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "SUNPHARMA",
        "equity_symbol": "SUNPHARMA",
        "sector":        "Pharma",
        "strike_step":   20,
        "lot_size":      400,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "LT",
        "equity_symbol": "LT",
        "sector":        "Engineering/Infra",
        "strike_step":   50,
        "lot_size":      175,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "SBIN",
        "equity_symbol": "SBIN",
        "sector":        "PSU Banking",
        "strike_step":   10,
        "lot_size":      750,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "BHARTIARTL",
        "equity_symbol": "BHARTIARTL",
        "sector":        "Telecom",
        "strike_step":   20,
        "lot_size":      475,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "TATASTEEL",
        "equity_symbol": "TATASTEEL",
        "sector":        "Metals",
        "strike_step":   2.5,
        "lot_size":      2750,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "ASIANPAINT",
        "equity_symbol": "ASIANPAINT",
        "sector":        "Paints/Consumer",
        "strike_step":   20,
        "lot_size":      250,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "AXISBANK",
        "equity_symbol": "AXISBANK",
        "sector":        "Private Banking",
        "strike_step":   10,
        "lot_size":      625,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "MARUTI",
        "equity_symbol": "MARUTI",
        "sector":        "Auto",
        "strike_step":   100,
        "lot_size":      50,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "CIPLA",
        "equity_symbol": "CIPLA",
        "sector":        "Pharma",
        "strike_step":   10,
        "lot_size":      375,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
    {
        "name":          "HINDALCO",
        "equity_symbol": "HINDALCO",
        "sector":        "Metals",
        "strike_step":   10,
        "lot_size":      700,
        "fno_exchange":  "NFO",
        "spot_exchange": "NSE",
    },
]

# Set of NSE equity tradingsymbols used in morning-login token caching.
STOCK_EQUITY_SYMBOLS = {s["equity_symbol"] for s in STOCKS}

# Lookup dict: name → config row.
STOCK_BY_NAME = {s["name"]: s for s in STOCKS}

# All stocks: monthly-only expiry. No weekly options post SEBI Nov 2024.
USE_WEEKLY = False

# Redis keys — separate namespace from index bot keys (which use no prefix).
REDIS_EQUITY_TOKENS_KEY = "kite:stock_equity_tokens"   # {symbol: instrument_token}
REDIS_OPTION_TOKENS_KEY = "kite:stock_option_tokens"   # {NAME_STRIKE_CE: {...}}

# Strike range (pts from spot) to pre-cache at morning-login (~5% of spot).
OPTION_CACHE_RANGE = {
    "RELIANCE":   150,
    "ICICIBANK":   60,
    "INFY":        80,
    "BAJFINANCE": 450,
    "SUNPHARMA":  100,
    "LT":         260,
    "SBIN":         50,
    "BHARTIARTL":  100,
    "TATASTEEL":    15,
    "ASIANPAINT":  140,
    "AXISBANK":     60,
    "MARUTI":      800,
    "CIPLA":        80,
    "HINDALCO":     50,
}

# DI threshold for C4 — lower than the index threshold (25); stocks are
# individually noisier and a 25 floor was filtering out otherwise-clean signals.
DI_THRESHOLD = 24

# Event exclusion — stocks with earnings/dividend/corp-action events in the
# next N calendar days (inclusive of today) are skipped entirely by stock_main.
EVENT_LOOKAHEAD_DAYS = 1
REDIS_EVENT_EXCLUDED_PREFIX = "stock:event_excluded"   # + ":{YYYY-MM-DD}"
