"""
stock_config.py — Configuration for the 7-stock F&O signal bot.

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
        "name":          "M&M",
        "equity_symbol": "M&M",
        "sector":        "Auto",
        "strike_step":   50,
        "lot_size":      175,
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
        "name":          "ITC",
        "equity_symbol": "ITC",
        "sector":        "FMCG",
        "strike_step":   10,
        "lot_size":      1600,
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
    "M&M":        160,
    "LT":         175,
    "SBIN":         50,
    "BHARTIARTL":  100,
    "ITC":          20,
    "TATASTEEL":    10,
    "ASIANPAINT":  140,
}

# DI threshold for C4 — lower than the index threshold (25); stocks are
# individually noisier and a 25 floor was filtering out otherwise-clean signals.
DI_THRESHOLD = 24

# Event exclusion — stocks with earnings/dividend/corp-action events in the
# next N calendar days (inclusive of today) are skipped entirely by stock_main.
EVENT_LOOKAHEAD_DAYS = 3
REDIS_EVENT_EXCLUDED_PREFIX = "stock:event_excluded"   # + ":{YYYY-MM-DD}"
