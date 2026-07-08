"""Static stock → sector → NSE sectoral index map. Hardcoded — re-verify
against NSE if any index is renamed/restructured (semi-annual review)."""

STOCK_SECTOR = {
    "RELIANCE":    "OIL_GAS",
    "ICICIBANK":   "PRIVATE_BANK",
    "INFY":        "IT",
    "BAJFINANCE":  "FINANCIAL_SERVICES",
    "SUNPHARMA":   "PHARMA",
    "LT":          "INFRA",
    "SBIN":        "PSU_BANK",
    "BHARTIARTL":  "TELECOM",
    "ITC":         "FMCG",
    "TATASTEEL":   "METAL",
    "ASIANPAINT":  "CONSUMER_DURABLES",
    "AXISBANK":    "PRIVATE_BANK",
    "MARUTI":      "AUTO",
    "CIPLA":       "PHARMA",
    "HINDALCO":    "METAL",
}

# Kite NSE tradingsymbol for each sector index. Verified 2026-06-30 against
# live kite.instruments("NSE") dump (segment=INDICES), except TELECOM (added
# later — see note below).
# Corrections from spec draft:
#   PRIVATE_BANK: "NIFTY PRIVATE BANK" → "NIFTY PVT BANK" (live dump name)
#   CONSUMER_DURABLES: kept as "NIFTY CONSR DURBL" (this file's own verified
#            live-dump value) despite a later change-spec proposing
#            "NIFTY CONSUMPTION" instead — see 2026-07-08 change-spec review,
#            kept per explicit user decision pending a fresh live-Kite check.
#   TELECOM: no dedicated NSE telecom index exists. "NIFTY MEDIA" is used as
#            the closest available proxy for BHARTIARTL (added 2026-07-08,
#            per change-spec — not independently re-verified against a live
#            Kite dump this session).
# If Kite ltp()/historical_data() calls fail for one of these, the morning
# step logs a warning and that sector is omitted from the Redis map (silent
# degrade — see stock_main.py handling).
SECTOR_INDEX_SYMBOL = {
    "OIL_GAS":             "NIFTY OIL AND GAS",
    "PRIVATE_BANK":        "NIFTY PVT BANK",
    "IT":                  "NIFTY IT",
    "FINANCIAL_SERVICES":  "NIFTY FIN SERVICE",
    "PHARMA":              "NIFTY PHARMA",
    "INFRA":               "NIFTY INFRA",
    "PSU_BANK":            "NIFTY PSU BANK",
    "TELECOM":             "NIFTY MEDIA",
    "FMCG":                "NIFTY FMCG",
    "METAL":               "NIFTY METAL",
    "CONSUMER_DURABLES":   "NIFTY CONSR DURBL",
    "AUTO":                "NIFTY AUTO",
}

NIFTY50_SYMBOL = "NIFTY 50"

SPREAD_THRESHOLD_PCT = 0.5   # |spread| must exceed this to tag; else neutral
LOOKBACK_SESSIONS = 3        # close-to-close over 3 trading sessions
