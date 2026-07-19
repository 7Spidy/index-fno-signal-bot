"""Constants for the IV Rank iron condor paper-trading system (NIFTY only).

Fully isolated from src/config.py and the C1-C4 index/stock paper paths —
none of these Redis keys or constants are shared with any other module.
"""

# ── IV Rank gate ──
IV_RANK_THRESHOLD   = 30       # enter only when IV Rank >= this
IV_RANK_WINDOW_DAYS = 252      # trailing window for rank min/max
VIX_TRADINGSYMBOL   = "INDIA VIX"   # confirmed via search_instruments (NSE)
VIX_EXCHANGE        = "NSE"

# ── Capital / sizing ──
CONDOR_CAPITAL       = 100_000  # total paper capital for this system
CAPITAL_RESERVE_FRAC = 0.0      # ZERO reserve — deploy 100% (per explicit instruction)
NIFTY_LOT_SIZE       = 75       # confirm current before go-live

# ── Structure (tight, per walkthrough example — flagged aggressive) ──
SHORT_OTM_OFFSET_PTS = 75       # short strikes ~75pt OTM each side from spot
SPREAD_WIDTH_PTS     = 100      # long strike = short strike +/- this
STRIKE_STEP          = 50       # NIFTY strike granularity

# ── Exit / theoretical target ──
# T is theoretical target distance per SHORT leg, expressed as fraction of that
# leg's entry premium that we aim to capture as decay. Judgement call: aim to
# capture 60% of each short leg's premium (best profit-target frac from the sweep).
SHORT_LEG_TARGET_CAPTURE_FRAC = 0.60
# Combined position exits when EITHER: combined unrealized P&L >= T_total (sum of
# per-short-leg targets) OR a short-leg trailing SL is hit (see exit logic).

# ── Liquidity scan thresholds ──
MIN_OI_LOTS = 1000     # skip strikes with OI below this (matches Kite nudge)
MAX_STRIKE_SCAN_STEPS = 3   # monotonic-order check is structural, not a constant

# ── Redis keys (all NEW, none reused from stock/index state) ──
REDIS_VIX_HISTORY_KEY = "condor:vix_history"    # JSON: {"YYYY-MM-DD": vix_close}
REDIS_CONDOR_POSITION = "condor:position:open"  # JSON full state, NO TTL while open
REDIS_CONDOR_LOCK     = "condor:position:lock"  # "1" while a position is open
REDIS_CONDOR_MSG_ID   = "condor:discord:msg_id" # persistent Discord msg id per position

# ── Timing ──
ENTRY_DECISION_HHMM = "09:30"  # morning entry runs after VIX settles
MARKET_CLOSE_HHMM   = "15:30"  # last tracker update of the day (no square-off)
