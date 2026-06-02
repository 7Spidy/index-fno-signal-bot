# CLAUDE.md — Index F&O Signal Bot

> **For Claude Code:** Read this file AND `fno-signal-bot-spec-v3.html` completely
> before writing any code. This file is the authoritative build guide.
> The HTML spec has the architecture diagrams, indicator math, and signal logic.
> This file has the exact build order, coding standards, and constraints.

---

## What This Project Does

A Python service that runs on GitHub Actions every 5 minutes from 09:40–14:45 IST
on NSE trading days. It fetches 5-minute OHLCV candles for NIFTY, BANKNIFTY,
FINNIFTY, and MIDCPNIFTY futures from Zerodha Kite Connect, computes session VWAP,
RSI(14), and DMI(14) using Wilder smoothing, evaluates 4 CE (bullish) and 4 PE
(bearish) conditions per instrument, and:

1. Writes `docs/dashboard.json` after EVERY run (signal or not) and pushes to git
2. Sends a Discord webhook message ONLY when all 4 conditions fire for CE or PE
3. Logs each signal to Notion (optional)

The `docs/` folder is served as a GitHub Pages site. `docs/index.html` is a
live dashboard that auto-polls `dashboard.json` every 5 minutes.

**This app NEVER places orders.** Alert only. Human decides to trade.

---

## Pre-existing Files (do NOT recreate these)

The following files are already provided in the repo. Copy them to the correct
locations without modification:

| Provided file | Copy to |
|---|---|
| `fno-signal-bot-spec-v3.html` | Reference only — do not copy to repo |
| `holidays_2026.json` | `holidays_2026.json` (repo root) |
| `seed_dashboard.json` | `docs/dashboard.json` |
| `verify_setup.py` | `verify_setup.py` (repo root) |

---

## Tech Stack

- **Python 3.11** — all source files must use Python 3.11+ syntax
- **`kiteconnect`** — official Zerodha SDK for auth and historical data
- **`pyotp`** — TOTP 2FA for automated daily Kite login
- **`pandas`** — DataFrame handling for OHLCV
- **`numpy`** — numerical operations in indicators
- **`requests`** — Upstash Redis REST API + Discord webhook
- **`python-dateutil`** — timezone handling (IST = UTC+5:30)
- **No other third-party libraries.** Do NOT use pandas-ta, ta-lib, or any
  other indicator library. Implement VWAP, RSI, and DMI from scratch per
  the exact specs in `fno-signal-bot-spec-v3.html` §7.

---

## Exact File Structure to Create

```
index-fno-signal-bot/
├── .github/workflows/
│   ├── morning-login.yml
│   └── signal.yml
├── docs/
│   ├── index.html          ← live dashboard (mobile-first)
│   ├── dashboard.json      ← copy from seed_dashboard.json
│   └── _headers            ← cache control
├── src/
│   ├── __init__.py         ← empty
│   ├── config.py
│   ├── auth.py
│   ├── kite_client.py
│   ├── indicators.py
│   ├── signals.py
│   ├── state.py
│   ├── dashboard_writer.py
│   ├── notifier.py
│   ├── journal.py
│   ├── calendar_nse.py
│   └── main.py
├── holidays_2026.json      ← copy from provided file
├── verify_setup.py         ← copy from provided file
├── requirements.txt
├── .env.example
└── README.md
```

---

## Environment Variables

All secrets come from GitHub Actions secrets (or a local `.env` file for testing).
Never hardcode any values. Always use `os.environ.get()`.

```
KITE_API_KEY          — Kite Connect app API key
KITE_API_SECRET       — Kite Connect app API secret (morning-login only)
KITE_USER_ID          — Zerodha login ID (e.g. IZ3912)
KITE_PASSWORD         — Zerodha login password (morning-login only)
KITE_TOTP_SECRET      — base32 TOTP seed (morning-login only)
UPSTASH_REDIS_REST_URL    — Upstash Redis REST endpoint
UPSTASH_REDIS_REST_TOKEN  — Upstash Redis REST token
DISCORD_WEBHOOK_URL   — Discord channel webhook URL
NOTION_TOKEN          — Notion integration token (optional)
NOTION_DB_ID          — Notion database ID (optional)
GITHUB_TOKEN          — auto-provided by Actions for git push (do not add to secrets)
```

---

## Build Order — Follow Exactly

Build and test each step before moving to the next.

### Step 1 — `requirements.txt`

```
kiteconnect
pyotp
pandas
numpy
requests
python-dateutil
python-dotenv
```

### Step 2 — `docs/_headers`

```
/dashboard.json
  Cache-Control: no-cache, no-store, must-revalidate
  Pragma: no-cache
  Expires: 0
```

### Step 3 — `src/state.py` — Upstash Redis via REST

All Redis calls use the Upstash REST API (HTTPS GET/POST). No redis-py client.
Implement these functions:

```python
def redis_get(key: str) -> str | None
def redis_set(key: str, value: str, ex: int | None = None) -> bool
def redis_delete(key: str) -> bool
def redis_exists(key: str) -> bool
```

Use `requests.get/post` with `Authorization: Bearer {UPSTASH_REDIS_REST_TOKEN}`.
Base URL: `UPSTASH_REDIS_REST_URL`.
Endpoints: `GET /get/{key}`, `POST /set/{key}/{value}`, `POST /del/{key}`.
For TTL: `POST /set/{key}/{value}?ex={seconds}`.
Return `None` / `False` on any error — never raise from state.py.

**Test checkpoint:** Run `python -m src.state` and verify a dummy key can be set
and retrieved before proceeding.

### Step 4 — `src/auth.py` — Kite TOTP Login

Implements `get_access_token() -> str` and `run_morning_login()`.

The automated Kite login flow using `requests.Session()`:

```python
import requests, pyotp
from kiteconnect import KiteConnect

def get_access_token(api_key, api_secret, user_id, password, totp_secret) -> str:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: password login
    r1 = session.post("https://kite.zerodha.com/api/login",
                      data={"user_id": user_id, "password": password})
    r1.raise_for_status()
    request_id = r1.json()["data"]["request_id"]

    # Step 2: TOTP
    totp_code = pyotp.TOTP(totp_secret).now()
    r2 = session.post("https://kite.zerodha.com/api/twofa",
                      data={"user_id": user_id, "request_id": request_id,
                            "twofa_value": totp_code, "twofa_type": "totp"})
    r2.raise_for_status()

    # Step 3: follow login URL to get request_token from redirect
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    r3 = session.get(login_url, allow_redirects=True)
    # request_token is in the final redirect URL as a query param
    from urllib.parse import urlparse, parse_qs
    params = parse_qs(urlparse(r3.url).query)
    request_token = params["request_token"][0]

    # Step 4: generate session
    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]
```

`run_morning_login()` must:
1. Call `get_access_token()`
2. Store in Redis: `redis_set("kite:access_token", token, ex=43200)` (12h TTL)
3. Store refresh time: `redis_set("kite:token_refreshed_at", iso_timestamp)`
4. On failure: post a Discord embed with title "⚠️ Kite Login Failed" and exit 1

**Test checkpoint:** Trigger `morning-login.yml` via `workflow_dispatch`.
Check Upstash console — `kite:access_token` key must appear.

### Step 5 — `src/kite_client.py` — Data Fetching

Implements:

```python
def get_kite() -> KiteConnect
    # reads kite:access_token from Redis, calls kite.set_access_token()

def resolve_futures_tokens() -> dict
    # calls kite.instruments("NFO"), filters FUT + near-month expiry
    # returns {"NIFTY": {"token": int, "tradingsymbol": str, "strike_step": int}, ...}
    # stores result in Redis as JSON: redis_set("kite:instrument_tokens", json, ex=86400)

def fetch_ohlcv(instrument_token: int, today_open: datetime) -> pd.DataFrame
    # fetches from prior_session_start to now using "5minute" interval
    # prior_session_start = today_open - timedelta(hours=3)  (last 36 prior candles)
    # returns DataFrame with columns: [timestamp, open, high, low, close, volume]
    # sorted ascending by timestamp
```

For `resolve_futures_tokens`, near-month logic:
```python
from datetime import date
futs = [i for i in instruments
        if i["name"] == underlying
        and i["instrument_type"] == "FUT"
        and i["expiry"] >= date.today()]
nearest = min(futs, key=lambda x: x["expiry"])
```

INSTRUMENTS config (hardcode in `src/config.py`):
```python
INSTRUMENTS = [
    {"name": "NIFTY",      "strike_step": 50},
    {"name": "BANKNIFTY",  "strike_step": 100},
    {"name": "FINNIFTY",   "strike_step": 50},
    {"name": "MIDCPNIFTY", "strike_step": 25},
]
```

### Step 6 — `src/indicators.py` — VWAP, RSI, DMI

Implement exactly as specified in `fno-signal-bot-spec-v3.html` §7.
All functions take a pandas DataFrame and return a pandas Series.

```python
def vwap_session(df: pd.DataFrame, session_open: datetime) -> pd.Series:
    """Session-anchored VWAP using HLC3 typical price.
    Only cumulates from session_open onwards. Prior candles get NaN.
    df must have columns: high, low, close, volume, timestamp"""

def rsi_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed RSI. Seed = simple avg of first `period` changes."""

def dmi_wilder(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (+DI, -DI, ADX) as three Series, Wilder-smoothed."""
```

**Critical:** The full DataFrame passed to these functions includes the prior
session warm-up candles. VWAP must use `session_open` to reset its cumulative
sums. RSI and DMI use the entire DataFrame (including prior session) for
Wilder smoothing, which gives them ~80 candles of warm-up.

**Test checkpoint:** Feed the DataFrame from the May 29 backtest. Verify:
- VWAP at 12:10 ≈ 24,006 (±5)
- -DI at 12:10 ≈ 30–31 (should be > 25, matching TV)

### Step 7 — `src/signals.py` — CE/PE Evaluation

```python
def evaluate(df: pd.DataFrame, vwap: pd.Series, rsi: pd.Series,
             pdi: pd.Series, ndi: pd.Series, cfg: dict) -> dict:
    """
    Evaluates CE and PE conditions on the latest fully-closed candle.
    Returns:
    {
      "ce": {"c1": bool, "c2": bool, "c3": bool, "c4": bool, "signal": bool},
      "pe": {"c1": bool, "c2": bool, "c3": bool, "c4": bool, "signal": bool},
      "price": float, "vwap": float, "rsi": float,
      "pdi": float, "ndi": float, "atm_strike": int
    }
    """
```

Latest closed candle = second-to-last row of df (last row may be forming).
Use `df.iloc[-2]` as `c[0]` and `df.iloc[-3]` as `c[1]`.

VWAP cross logic (C2): look back `VWAP_CROSS_WINDOW_CANDLES` candles from `c[0]`.
For CE: `c[0].close > vwap[-2]` AND exists k in 1..6 where `df.iloc[-2-k].close <= vwap.iloc[-2-k]`.
Direction must match (CE = cross-up, PE = cross-down).

Guard: if both ce.signal and pe.signal are True — set both to False and log warning.
This should never happen but is a safety net.

Config dict keys (from `src/config.py`):
```python
MOMENTUM_RULE = "close_gt_prev_close"   # or "open_gt_prev_close"
RSI_SLOPE_LOOKBACK = 3
VWAP_CROSS_WINDOW_CANDLES = 6
DI_THRESHOLD = 25
REQUIRE_DI_DOMINANCE = True
USE_ADX_FILTER = False
ADX_MIN = 20
COOLDOWN_CANDLES = 3
SESSION_START_IST = "09:15"
EVAL_WINDOW_IST = ("09:40", "14:45")
```

### Step 8 — `docs/dashboard.json` seed

Copy `seed_dashboard.json` to `docs/dashboard.json`. This prevents a 404 on
first page load before the first Actions run.

### Step 9 — `docs/index.html` — Live Dashboard

Mobile-first. Dark theme. Single HTML file, all CSS and JS inline. No build
step, no npm, no frameworks. Reads `./dashboard.json?t={Date.now()}` (cache-bust).

**Design tokens** (must use these exact CSS variables):
```css
--bg: #090c14; --surface: #0f1422; --surface2: #141929;
--border: #1e2840; --border2: #263050;
--green: #00e5a0; --green-dim: #00e5a015;
--red: #f87171;   --red-dim: #f8717115;
--amber: #f59e0b;
--text: #dde4f0;  --text-muted: #6b7fa3; --text-dim: #9aacc8;
--mono: 'JetBrains Mono', monospace;
--display: 'Syne', sans-serif;
```
Load both fonts from Google Fonts in the `<head>`.

**Layout sections (top to bottom on mobile):**
1. Header bar — app name, live green pulsing dot, last_run time + "X min ago"
   counter (updates every 30s), token status pill (🟢 Valid / 🔴 Refresh needed)
2. Signal banners — `display:none` by default. When `active_signals.length > 0`,
   show one full-width card per signal with pulsing glow animation on the border.
   Green for CE, red for PE. Shows instrument + suggested ATM strike.
3. Instrument cards — one per instrument in `data.instruments`.
   Each card: instrument name + futures price + direction arrow at top.
   Below: VWAP / RSI / +DI / -DI values as a small data row.
   Split 2-col grid: left = CE (4 conditions ✓/✗ with labels), right = PE (same).
   Bottom of each half: "● SIGNAL" (colored) or "── no signal" (muted).
4. History log — scrollable table. Columns: Time · Instrument · CE dots (4 coloured
   dots, green=pass dim=fail) · PE dots · signal indicator. Newest row at top.
   Max 280 rows (70 ticks × 4 instruments). Signal rows get a subtle left border.

**Polling:**
```javascript
async function fetchData() {
    const res = await fetch(`./dashboard.json?t=${Date.now()}`);
    const data = await res.json();
    render(data);
}
setInterval(fetchData, 5 * 60 * 1000);
setInterval(updateTimeAgo, 30 * 1000);
fetchData();
```

**Signal banner animation:**
```css
@keyframes pulse-green {
    0%, 100% { box-shadow: 0 0 0 0 rgba(0,229,160,0.45); }
    50%       { box-shadow: 0 0 0 16px rgba(0,229,160,0); }
}
```

**Condition labels for ✓/✗ display:**
- C1 CE: "Close ↑ prev" / C1 PE: "Close ↓ prev"
- C2 CE: "VWAP cross-up" / C2 PE: "VWAP cross-dn"
- C3 CE: "RSI rising" / C3 PE: "RSI falling"
- C4 CE: "+DI > 25" / C4 PE: "-DI > 25"

**Error state:** If fetch fails, show a subtle banner "Dashboard offline — retrying"
without breaking the layout.

### Step 10 — `src/dashboard_writer.py`

Loads `docs/dashboard.json`, updates it with latest run data, writes it back,
and commits + pushes to git. See §9 of `fno-signal-bot-spec-v3.html` for the
exact Python code — replicate it faithfully.

Key points:
- `load()` resets `history` to `[]` if `data["date"] != date.today().isoformat()`
- New history rows are **prepended** (newest first), list sliced to `MAX_HISTORY = 280`
- `_git_commit()` must check `git diff --staged --quiet` — only commit if changed
- Use `subprocess.run(..., check=True)` throughout
- Git user config: `actions@github.com` / `GitHub Actions`
- The `GITHUB_TOKEN` is automatically available in Actions as `secrets.GITHUB_TOKEN`
  and is pre-configured for git push — no extra setup needed

**Reset mode** (called from `morning-login.yml`):
```python
def reset_day():
    data = load()
    data["history"] = []
    data["date"] = date.today().isoformat()
    data["active_signals"] = []
    data["token_valid"] = True
    data["token_refreshed_at"] = datetime.now(IST).isoformat()
    FILE.write_text(json.dumps(data, indent=2))
    _git_commit(datetime.now(IST))
```

### Step 11 — `src/notifier.py` — Discord

Called ONLY when a signal fires. Builds and posts a rich Discord embed.
See `fno-signal-bot-spec-v3.html` §11 for exact embed format.

```python
def send_signal(instrument: str, direction: str, result: dict) -> bool:
    """Returns True if webhook POST succeeded (2xx)."""
```

Color: `0x00e5a0` for CE, `0xf87171` for PE.
Fields: Futures Price, ATM Strike, Candle time (IST), RSI, +DI/-DI, VWAP delta, Conditions.
Footer: "Alert only · verify before trading"
Always include `"timestamp": datetime.utcnow().isoformat() + "Z"` in the embed.

On HTTP error: log to stdout (Actions will capture it). Do not raise — a notifier
failure must never crash the main evaluation loop.

### Step 12 — `src/calendar_nse.py`

```python
def is_trading_day(d: date | None = None) -> bool:
    """Returns False for weekends and dates in holidays_2026.json"""

def in_eval_window(now: datetime | None = None) -> bool:
    """Returns True if now is between EVAL_WINDOW_IST[0] and EVAL_WINDOW_IST[1] IST"""
```

Load `holidays_2026.json` from the repo root. Parse dates as `date` objects.
IST timezone: `ZoneInfo("Asia/Kolkata")` (stdlib, Python 3.9+, no pytz needed).

### Step 13 — `src/main.py` — Orchestrator

```python
def main():
    # 1. Gate: trading day + eval window
    if not (calendar_nse.is_trading_day() and calendar_nse.in_eval_window()):
        print("Outside trading window — exiting")
        return

    # 2. Read token
    token = state.redis_get("kite:access_token")
    if not token:
        notifier.send_warning("⚠️ No access token in Redis. Run morning-login.yml.")
        return

    # 3. Read instrument tokens (cached by morning-login)
    raw = state.redis_get("kite:instrument_tokens")
    instrument_tokens = json.loads(raw) if raw else kite_client.resolve_futures_tokens()

    # 4. Session open for today (09:15 IST)
    today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)

    # 5. Per-instrument loop
    results = []
    for inst in config.INSTRUMENTS:
        name = inst["name"]
        token_info = instrument_tokens.get(name)
        if not token_info:
            print(f"No token for {name} — skipping")
            continue
        try:
            df = kite_client.fetch_ohlcv(token_info["token"], today_open)
            vwap = indicators.vwap_session(df, today_open)
            rsi  = indicators.rsi_wilder(df)
            pdi, ndi, adx = indicators.dmi_wilder(df)
            result = signals.evaluate(df, vwap, rsi, pdi, ndi, config.as_dict())
            result["name"] = name
            result["symbol"] = token_info["tradingsymbol"]
            result["strike_step"] = inst["strike_step"]
            results.append(result)

            # Signal + dedup + cooldown
            for direction in ("ce", "pe"):
                if result[direction]["signal"]:
                    candle_ts = df.iloc[-2]["timestamp"].isoformat()
                    dedup_key = f"fired:{name}:{direction}:{candle_ts}"
                    if not state.redis_exists(dedup_key):
                        cooldown_key = f"cooldown:{name}:{direction}"
                        last_fired = state.redis_get(cooldown_key)
                        # cooldown: skip if same instrument+direction fired <N candles ago
                        if not _within_cooldown(last_fired, df.iloc[-2]["timestamp"]):
                            notifier.send_signal(name, direction.upper(), result)
                            state.redis_set(dedup_key, "1", ex=86400)
                            state.redis_set(cooldown_key,
                                            df.iloc[-2]["timestamp"].isoformat())
        except Exception as e:
            print(f"ERROR processing {name}: {e}")
            # continue to next instrument — don't crash the loop

    # 6. Update dashboard
    dashboard_writer.update_and_commit(results)
```

### Step 14 — GitHub Actions Workflows

Copy exactly from `fno-signal-bot-spec-v3.html` §10.
Key things to get right:
- Both workflows need `permissions: contents: write`
- `signal.yml` needs `timeout-minutes: 4`
- `morning-login.yml` checkout needs `fetch-depth: 1`
- The `workflow_dispatch: {}` trigger must be on both (for manual testing)

### Step 15 — `src/journal.py` (Optional — build last)

Notion integration. Append a row to the signals database when a signal fires.
Only build this after everything else is working. It's completely optional.
The database should have these properties: Date, Time (IST), Instrument, Direction,
Futures Price, ATM Strike, RSI, +DI, -DI, VWAP Delta, Conditions (text), Signal.

### Step 16 — `README.md`

Minimal. Describe what the project does, how to set it up, and point to
`verify_setup.py` as the first step.

---

## Critical Constraints

1. **Never place orders.** The app is read-only from Kite's perspective.
2. **Never use `exit()` in the instrument loop.** Always `continue` on error.
3. **Always check `diff --staged --quiet` before git commit.** A no-op run (no data
   change) must not create an empty commit.
4. **VWAP resets at 09:15 IST daily.** Prior candles get `NaN` from `vwap_session()`.
5. **RSI and DMI use the full DataFrame** (including prior session tail) for Wilder
   warm-up. This is what makes DMI values match TradingView.
6. **Latest closed candle is `df.iloc[-2]`**, not `df.iloc[-1]` (last candle may be
   still forming when the script runs mid-5-min interval).
7. **ATM strike:** `round(price / strike_step) * strike_step` — use Python's
   `round()`, not `int()`. They differ for .5 cases.
8. **All times in IST.** Use `ZoneInfo("Asia/Kolkata")`. Log timestamps with
   timezone suffix so Actions logs are readable.
9. **The `GITHUB_TOKEN` for git push is auto-injected** by Actions. Do not add it
   to secrets. Do not hardcode it. It's available as the standard Actions token.

---

## Testing Approach

- Unit test `indicators.py` by replaying the May 29 backtest data. Expected:
  -DI at 12:10 ≈ 30.9, RSI at 12:10 ≈ 33.9, VWAP at 12:10 ≈ 24,006.7
- Test `signals.py` against the May 29 12:10 candle — must return PE signal = True
- Test `dashboard_writer.py` locally: run it, check `docs/dashboard.json` is updated
- Test `notifier.py` by calling `send_signal()` directly with dummy data — verify
  Discord embed appears in channel before wiring into main loop

---

## Build Checkpoints (do not skip)

After step 3 (state.py): `python -m src.state` — Redis round-trip must succeed
After step 4 (auth.py): `workflow_dispatch` morning-login.yml — token in Redis
After step 5 (kite_client.py): fetch NIFTY candles manually and print shape
After step 6 (indicators.py): unit test with May 29 data
After step 9 (dashboard HTML): open `docs/index.html` locally with a test JSON
After step 10 (dashboard_writer): verify git commit appears in repo history
After step 11 (notifier): test message in private Discord channel
After step 13 (main.py): `workflow_dispatch` signal.yml during market hours
After ALL steps: run `verify_setup.py` — all checks must pass
