# Index F&O Signal Bot

A Python service that monitors Indian index futures and individual stock options every 5 minutes during NSE market hours, evaluates a multi-condition technical signal model, and sends Discord alerts when a high-conviction CE (bullish) or PE (bearish) trade setup forms. It never places orders — human decides to trade.

**Index instruments:** NIFTY, BANKNIFTY, SENSEX futures
**Stock instruments:** 14 NSE-listed stocks (see [Stock Signal Bot](#stock-signal-bot) below)
**Exchange:** NFO (NIFTY/BANKNIFTY/stocks) and BFO (SENSEX)
**Runs via:** GitHub Actions (morning-login + signal evaluation) with cron-job.org as the 5-min trigger

---

## How It Works — Top Level

```
09:05 IST  morning-login.yml   →  Kite TOTP login → Redis token
                                   → index instrument + option cache
                                   → stock equity + option token cache
                                   → stock event exclusion check (Marketaux)
                                   → dashboard reset

09:40 IST  signal.yml (every 5 min until 14:45)          ← index bot
               │
               ├─ Gate: trading day? eval window?
               ├─ Fetch 5-min OHLCV candles (today + 5-day warm-up tail)
               ├─ Compute VWAP, RSI(14), DMI(14)
               ├─ Fetch live LTP + VWAP from Kite quote API
               ├─ Recompute live RSI + live DMI (synthetic bar)
               ├─ Evaluate 4-condition CE/PE model
               ├─ On signal: dedup + cooldown → Discord alert → dashboard → Notion journal
               └─ Always: write docs/dashboard.json → git push → GitHub Pages

09:40 IST  stock-signal.yml (every 5 min until 14:45)    ← stock bot
               │
               ├─ Gate: trading day? eval window? event-excluded?
               ├─ Fetch 5-min equity OHLCV candles (today + 5-day warm-up)
               ├─ Compute VWAP, RSI(14), DMI(14)
               ├─ Fetch live LTP + VWAP from Kite quote API
               ├─ Evaluate 4-condition CE/PE model per stock
               ├─ On signal: dedup + cooldown → Discord alert (#signals-stocks)
               └─ Always: write docs/stock-dashboard.json → git push

Every 1 min  trade-tracker.yml (triggered by cron-job.org → workflow_dispatch)
               │
               ├─ Poll Discord for /enter or /exit messages (Bot token REST API)
               ├─ /enter → snapshot open Kite positions → init track:* Redis keys
               ├─ /exit  → detect closed positions → post Exit summary → delete keys
               └─ Heartbeat (always): for each track:* key → fetch LTP → compute
                  trailing SL (mechanical ladder + RSI-based tightening) →
                  compare vs. actual Kite SL order → post FYI or ACTION embed
```

---

## Signal Logic — Detailed

All four conditions must pass simultaneously for a signal to fire. CE (Call Entry / bullish) and PE (Put Entry / bearish) are directional mirrors evaluated independently on every run.

### The 4 Conditions

#### C1 — Momentum

Checks whether price is still moving in the expected direction *right now*, using the live LTP vs. the most recently closed candle's close price.

```
CE:  live_ltp  >  P0.close      (price above last closed candle's close)
PE:  live_ltp  <  P0.close      (price below last closed candle's close)
```

P0 is always `df.iloc[-2]` — the second-to-last row — because the last candle may still be forming mid-5-minute interval. This is a core invariant throughout the codebase.

#### C2 — VWAP Position + Touch

Requires the live price to be on the correct side of the session VWAP *and* that the most recently closed candle touched or crossed through VWAP (meaning price just broke out from VWAP, not that it drifted there).

```
CE:  live_ltp > live_vwap   AND   P0.low  <= P0.vwap   (candle dipped to/below VWAP)
PE:  live_ltp < live_vwap   AND   P0.high >= P0.vwap   (candle spiked to/above VWAP)
```

The "P0 touched VWAP" clause is the recency filter — it ensures the VWAP interaction happened in the last closed candle, not hours ago.

Note: `VWAP_PROXIMITY_PTS` is defined in `config.py` and passed to the evaluator, but is not currently applied inside `signals.evaluate()`. It is a planned gate, not an active one.

#### C3 — RSI Trend (3 candles)

Requires a consistent RSI directional slope across three data points: the previous closed candle (P1), the latest closed candle (P0), and the live-updated RSI. No RSI threshold is applied — only the direction of the slope matters.

```
CE:  live_rsi > rsi[P0] > rsi[P1]     (RSI rising over last 3 checkpoints)
PE:  live_rsi < rsi[P0] < rsi[P1]     (RSI falling over last 3 checkpoints)
```

The live RSI is computed by appending a synthetic OHLC bar built from P0.close → live_ltp to the historical DataFrame and rerunning the Wilder smoothing algorithm on the extended series. This gives a live-updated RSI without waiting for a candle to close.

#### C4 — DMI Dominance + Rising Trend

The Directional Movement Index condition has three parts:

1. The dominant DI line must be above the threshold (25)
2. The dominant DI must be larger than the opposing DI (dominance)
3. The dominant DI must be rising: live_DI > DI[P0] > DI[P1] (3-point trend)

```
CE:  live_pdi > 25   AND   live_pdi > live_ndi   AND   live_pdi > pdi[P0] > pdi[P1]
PE:  live_ndi > 25   AND   live_ndi > live_pdi   AND   live_ndi > ndi[P0] > ndi[P1]
```

Like RSI, the live DMI values are computed by rerunning `dmi_wilder()` on the DataFrame extended with a synthetic bar at the live price. This means C4 reflects whether the directional trend is *building right now*, not just where it stood at the last candle close.

ADX filter is currently disabled (`USE_ADX_FILTER = False`) — ADX is computed but not used for gating.

### Guard: Mutual Exclusion

If both CE and PE pass all 4 conditions simultaneously (theoretically impossible but defensively coded), both signals are suppressed and a warning is logged.

### Summary Table

| # | Condition | CE (bullish) | PE (bearish) |
|---|---|---|---|
| C1 | Momentum | Live price > P0 close | Live price < P0 close |
| C2 | VWAP position + touch | Live > VWAP, P0 touched from below | Live < VWAP, P0 touched from above |
| C3 | RSI slope (3 points) | live_RSI > P0_RSI > P1_RSI | live_RSI < P0_RSI < P1_RSI |
| C4 | DI dominance + rising | +DI > 25, +DI > −DI, +DI rising | −DI > 25, −DI > +DI, −DI rising |

---

## Indicator Math

All indicators are implemented from scratch in `src/indicators.py`. No ta-lib or pandas-ta.

### VWAP — Session-Anchored

Session VWAP resets at 09:15 IST each day. Prior-session candles (the warm-up tail fetched from the previous days) receive `NaN` for VWAP — they contribute to RSI/DMI warm-up but not to VWAP.

```
Typical price  =  (High + Low + Close) / 3        ← HLC3
VWAP           =  cumsum(TP × Volume) / cumsum(Volume)
                  where cumsum resets at 09:15 IST
```

### RSI(14) — Wilder Smoothing

Uses the Wilder exponential moving average, not simple EMA. The seed is the simple average of the first 14 up/down moves; from bar 15 onward, each value is the Wilder-smoothed update.

```
α  =  1/14

Seed:   avg_gain  =  mean(gains[0:14])
        avg_loss  =  mean(losses[0:14])

Update (each subsequent bar):
        avg_gain  =  avg_gain × (1 − α) + gain × α
        avg_loss  =  avg_loss × (1 − α) + loss × α

RS    =  avg_gain / avg_loss
RSI   =  100 − (100 / (1 + RS))
```

Because RSI and DMI are computed on the full historical DataFrame (including prior-session warm-up candles fetched from the last 5 calendar days), the indicators have approximately 80+ candles of warm-up before the first in-session candle, which is what makes DMI values match TradingView.

### DMI(14) — Wilder Smoothing

True Range, +DM and −DM are computed per bar, then Wilder-smoothed with a 14-period rolling sum (not a simple average).

```
Up move    =  High[i] − High[i-1]
Down move  =  Low[i-1] − Low[i]

+DM[i]  =  Up   if Up > Down and Up > 0     else 0
−DM[i]  =  Down if Down > Up and Down > 0   else 0
TR[i]   =  max(High−Low, |High−PrevClose|, |Low−PrevClose|)

Wilder smoothing (after seed from first 14 bars):
    ATR     =  ATR  − ATR/14 + TR[i]
    +DM_s   =  +DM_s − +DM_s/14 + +DM[i]
    −DM_s   =  −DM_s − −DM_s/14 + −DM[i]

+DI  =  100 × +DM_s / ATR
−DI  =  100 × −DM_s / ATR

DX   =  100 × |+DI − −DI| / (+DI + −DI)
ADX  =  Wilder smooth of DX over 14 bars (seed = avg of first 14 DX values)
```

### Live Bar Synthesis

Between candle closes, all indicator values are updated using a synthetic bar:

```python
live_bar = {
    open:   P0.close,
    high:   max(P0.close, live_ltp),
    low:    min(P0.close, live_ltp),
    close:  live_ltp,
    volume: 0,
}
```

This synthetic bar replaces the untrusted partial last row in the DataFrame. RSI and DMI are then recomputed on the extended series, and `.iloc[-1]` gives the live-updated indicator value. Volume is irrelevant to RSI/DMI (price-range only) and is set to 0.

---

## Trade Details Computed on Signal

When all 4 conditions pass, the bot computes full trade parameters before sending the alert:

### ATM Strike

```python
atm_strike = round(spot_price / strike_step) * strike_step
```

Uses Python's `round()` (banker's rounding) not `int()`. Strike step by instrument:
- NIFTY: 50 pts
- BANKNIFTY: 100 pts
- SENSEX: 100 pts

### Conviction Label

Based on the spread between the dominant and opposing DI at signal time:

| Spread | Label |
|---|---|
| ≥ 18 | Strong |
| ≥ 10 | Moderate |
| < 10 | Building |

For CE signals: spread = +DI − −DI. For PE signals: spread = −DI − +DI.

### Stop Loss (Spot-Based)

Anchored to the structural extreme of the *previous* closed candle (P1), not the current one. This gives a structural level that the market has already confirmed, and avoids placing SL inside the signal candle itself.

```
CE:  spot_sl  =  P1.low    (round to 0.1)
PE:  spot_sl  =  P1.high   (round to 0.1)

raw_risk  =  max(|reference − spot_sl|, min_risk)
             min_risk per instrument: NIFTY=10, BANKNIFTY/SENSEX=30 pts
```

### Target (1.5 R:R)

```
CE:  spot_target  =  reference + 1.5 × raw_risk
PE:  spot_target  =  reference − 1.5 × raw_risk
```

The 1.5 R:R was chosen after backtesting NIFTY signals (May 22 – Jun 5, 2026, 17 signals): break-even win rate at 1.5R = 40%, measured win rate ≈ 41%. This is a thin edge — monitor per-index results on paper before live trading.

### Option Premium SL and Target

ATM option LTP is fetched live from Kite at signal time. SL and target are then converted from spot-point terms to option-premium terms using an ATM delta approximation:

```
ATM delta  =  0.50   (standard approximation for liquid ATM index options)

opt_sl      =  atm_ltp − raw_risk × delta
opt_target  =  atm_ltp + raw_risk × 1.5 × delta
```

These are the levels shown in the Discord alert.

---

## Deduplication and Cooldown

Two mechanisms prevent duplicate alerts:

**Deduplication** — A key `fired:{instrument}:{direction}:{candle_timestamp}` is written to Redis with a 24-hour TTL the moment a signal fires. If the same candle fires twice (e.g. due to retries or overlapping cron triggers), the second alert is suppressed.

**Cooldown** — A key `cooldown:{instrument}:{direction}` stores the last fired candle's timestamp. If the next signal would fire within `COOLDOWN_CANDLES × 5 minutes` (currently 3 candles = 15 minutes) of the last, it is suppressed. This prevents a rapid sequence of alerts on a noisy breakout.

---

## Trailing SL Position Tracker

An advisory-only monitoring module that tracks manually-executed trades in real time. It never places, modifies, or cancels orders — you always act in the Kite app; this system only observes and informs.

**Trigger:** cron-job.org fires `trade-tracker.yml` via `workflow_dispatch` every minute.

### How to use it

1. Take a trade manually in Kite after a signal fires.
2. Type `/enter` in the designated Discord channel — the tracker snapshots all open positions from Kite and starts monitoring.
3. Every minute: receive a **FYI** (blue) embed if your Kite SL is already at the required level, or an **ACTION** (amber) embed telling you exactly what to move your SL to and why.
4. When you exit the trade in Kite, type `/exit` — the tracker detects the closed position and posts an **Exit summary** (P&L, R-multiple, SL compliance ratio).

### Trailing SL ladder

Progress toward target T determines the SL floor. The ladder is monotonic — once the SL is raised (CE) or lowered (PE), it never reverses.

| Progress toward T | SL fraction of T locked in |
|---|---|
| < 0.5 T | No change — hold original SL |
| ≥ 0.5 T | 0.25 T above entry |
| ≥ 0.9 T | 0.60 T above entry |
| ≥ 1.0 T | 0.90 T above entry |
| ≥ 1.0 T + 0.1·n | (0.90 + 0.10·n) T above entry — locks in further profit every +0.1T step |

For PE positions, "above entry" is reversed to "below entry".

### AI-adjusted tightening (v1 — rule-based)

After the ladder SL is computed, a deterministic heuristic may tighten it further — but can **never loosen** it:

- If RSI shows a 3-point reversal against the position's direction **and** progress ≥ 0.7T, the SL is tightened to `current_price ∓ 0.05T`.
- Otherwise, the ladder SL is used unchanged.

This is explicitly v1. A multi-factor scoring version is a planned future iteration.

### Redis keys (position tracker)

| Key | Content | TTL |
|---|---|---|
| `track:{instrument}:{date}` | JSON: entry, T, direction, prior\_sl, last\_alert\_sl, compliance counters | 24 hours |
| `trade:last_seen_msg_id` | Discord message ID — poll cursor for /enter / /exit detection | none |

### New GitHub Secrets required (manual setup — not part of the code)

| Secret | Description |
|---|---|
| `DISCORD_BOT_TOKEN` | Discord Bot token for reading messages via REST API |
| `TRADE_TRACKER_CHANNEL_ID` | Discord channel ID where /enter and /exit are typed |
| `DISCORD_TRADE_TRACKER_WEBHOOK_URL` | Webhook URL for posting FYI / ACTION / Exit embeds |

Note: reading messages requires a Bot token; posting embeds uses a plain webhook. These are intentionally separate so the tracker never needs message-send permission scoping on the bot.

---

## Stock Signal Bot

A parallel signal bot running the same 4-condition model on 14 individual NSE stocks. Signal logic, indicator math, and trade computation are identical to the index bot — same `indicators.py`, same `signals.py`. The stock bot uses equity OHLCV data (NSE tokens, real volume) but resolves ATM strikes and option tokens from the NFO monthly chain.

### Stock Universe (14 stocks)

| Stock | Sector | Strike Step | Lot Size |
|---|---|---|---|
| RELIANCE | Energy/Conglomerate | 50 | 250 |
| ICICIBANK | Private Banking | 20 | 700 |
| INFY | IT | 20 | 400 |
| BAJFINANCE | NBFC | 100 | 125 |
| SUNPHARMA | Pharma | 20 | 400 |
| LT | Engineering/Infra | 50 | 175 |
| SBIN | PSU Banking | 10 | 750 |
| BHARTIARTL | Telecom | 20 | 475 |
| ITC | FMCG | 10 | 1600 |
| TATASTEEL | Metals | 2.5 | 2750 |
| ASIANPAINT | Paints/Consumer | 20 | 250 |

All stocks use **monthly expiry only** (no weekly — post SEBI Nov 2024 restriction). Lot sizes should be verified quarterly from `kite.instruments("NFO")` as NSE revises them.

### Corporate Event Exclusion (Marketaux)

Each morning, `stock_events.py` queries the [Marketaux](https://www.marketaux.com/) `news/all` API for recent news matching event-language keywords (board meeting, AGM, demerger, buyback, bonus/rights issue, M&A, stock split, IPO, delisting). Any stock with a matching article published within the last calendar day is written to a Redis exclusion list and skipped by `stock_main.py` for that trading day.

**Known limitation:** Marketaux's search is backward-looking — it surfaces news already published, not a structured forward calendar date. A board-meeting announcement in yesterday's article is a proxy for an upcoming event, not a guarantee. This is an accepted trade-off after both prior data sources became unusable (NSE `top-corp-info` blocked by Akamai; Yahoo Finance crumb handshake returns 406 as of 2026-06-20).

Redis key: `stock:event_excluded:{YYYY-MM-DD}` → JSON list of excluded stock names. A summary embed is posted to `#signals-stocks` after every morning run — success, partial, or failure — visually distinct from live trading alerts by its `"Stock Event News"` title and `stock_events (marketaux)` footer.

---

## Data Pipeline

### Morning Login (~09:05 IST)

`morning-login.yml` runs once per trading day before market open:

1. **TOTP login** — `src/auth.py` automates the Zerodha 3-step login (password → TOTP 2FA → request token extraction) using `requests.Session`. The access token is stored in Upstash Redis with a 12-hour TTL.
2. **Index futures instrument cache** — Resolves the nearest-expiry futures contract for each index instrument from the Kite NFO/BFO dump and stores the token map in Redis (24-hour TTL). Expiry logic: NIFTY uses weekly (Tuesday), BANKNIFTY and SENSEX use monthly (nearest calendar resolution).
3. **Index option token cache** — Pre-caches ATM ± range option tokens around current spot for each index instrument:
   - NIFTY: ±500 pts from spot
   - BANKNIFTY: ±1,500 pts
   - SENSEX: ±2,000 pts
4. **Dashboard reset** — Clears today's history in `docs/dashboard.json` and pushes a clean slate to git.
5. **Stock equity token cache** — `src/stock_kite_client.py --cache-equity-tokens` resolves NSE equity instrument tokens for all 14 tracked stocks and stores them in Redis.
6. **Stock option token cache** — `src/stock_kite_client.py --cache-stock-options` pre-caches monthly ATM ± range option tokens for each stock.
7. **Stock event exclusion** — `src/stock_events.py --cache-event-exclusions` queries Marketaux for corporate event news and writes the exclusion list to Redis. Runs with `continue-on-error: true` — a Marketaux failure is reported to Discord but never blocks the rest of morning-login.

### Signal Evaluation (~09:40–14:45 IST, every 5 min)

`signal.yml` is triggered every 5 minutes by cron-job.org (GitHub Actions native cron is too imprecise for market data).

For each instrument:

1. **Candle freshness guard** — Computes the expected timestamp of the candle that should have just closed. If Kite's response is lagging, retries once after 5 seconds. Skips the instrument if the candle is still stale after retry.
2. **OHLCV fetch** — `kite_client.fetch_ohlcv()` fetches 5-minute bars from 5 calendar days prior through the current moment. This gives ~80 prior-session candles for Wilder warm-up.
3. **Indicator computation** — VWAP, RSI(14), DMI(14) computed on the full DataFrame.
4. **Live quote** — `kite_client.get_live_quote()` fetches live LTP and VWAP from the Kite quote API.
5. **Live indicator update** — `indicators.with_live_bar()` builds a synthetic bar and `rsi_wilder()` / `dmi_wilder()` are rerun to produce live RSI, +DI, −DI.
6. **Signal evaluation** — `signals.evaluate()` checks all 4 conditions using both closed-candle and live values.
7. **Trade computation** (on signal) — spot LTP, ATM option LTP, SL, target, delta, conviction.
8. **Alert + record** — Discord webhook, dashboard update, Notion journal entry, Redis dedup keys.
9. **Dashboard commit** — `docs/dashboard.json` is updated and git-pushed regardless of whether a signal fired.

---

## Output: Discord Alert

A signal embed contains:

- **Instrument name + direction** (e.g. "NIFTY CE Signal")
- **Option contract** to buy (e.g. `NIFTY25JUN24750CE`) with expiry
- **Buy at** — live ATM option LTP at signal time
- **Target** — option premium target (spot-based R:R converted via delta)
- **Stop Loss** — option premium SL (structural level converted via delta)
- **Conviction** — Strong / Moderate / Building + R:R ratio (e.g. "Strong · 1:1.5")
- **Futures price** + spot-futures spread (if >5 pts)
- **Candle time** (IST)
- **RSI(14)** value
- **+DI / −DI** values
- **VWAP** value + whether price is above or below
- **Conditions** — ✅/❌ checklist showing which of the 4 conditions passed

Footer: "Alert only · Buy/Target/SL are option premium levels · verify before trading"

---

## Output: Live Dashboard

`docs/index.html` is served via GitHub Pages and auto-polls `dashboard.json` every 5 minutes.

**Layout (mobile-first, dark theme):**
- Header: app name, live status dot, last-run time, "X min ago" counter (updates every 30s), token validity pill
- Signal banners: pulsing glow cards when active signals exist (green for CE, red for PE)
- Instrument cards: futures price, direction arrow, VWAP/RSI/+DI/−DI values, 2-column CE/PE condition grid (✓/✗ per condition), signal status
- History log: scrollable table (newest first, max 400 rows), 4 condition dots per side, signal indicator

---

## Architecture: State Management

All ephemeral state lives in Upstash Redis (REST API — no redis-py, no persistent connection). Key schema:

| Key | Content | TTL |
|---|---|---|
| `kite:access_token` | Kite access token string | 12 hours |
| `kite:token_refreshed_at` | ISO timestamp of last login | none |
| `kite:instrument_tokens` | JSON dict: name → {token, tradingsymbol} | 24 hours |
| `kite:option_tokens:{name}` | JSON list of pre-cached index option contracts | 24 hours |
| `kite:stock_equity_tokens` | JSON dict: symbol → instrument_token | 24 hours |
| `kite:stock_option_tokens` | JSON dict: NAME_STRIKE_CE → {token, ...} | 24 hours |
| `stock:event_excluded:{date}` | JSON list of event-excluded stock names | 18 hours |
| `fired:{name}:{dir}:{ts}` | Dedup sentinel (value "1") | 24 hours |
| `cooldown:{name}:{dir}` | ISO timestamp of last fired candle | none |
| `track:{instrument}:{date}` | Position tracker state: entry, T, direction, prior\_sl, last\_alert\_sl, compliance counters | 24 hours |
| `trade:last_seen_msg_id` | Discord poll cursor — last processed message ID for /enter / /exit | none |

---

## File Structure

```
index-fno-signal-bot/
├── .github/workflows/
│   ├── morning-login.yml      daily ~09:05 IST — login + cache + reset (index + stock)
│   ├── signal.yml             5-min index evaluation loop via workflow_dispatch
│   ├── stock-signal.yml       5-min stock evaluation loop via workflow_dispatch
│   └── trade-tracker.yml      1-min position tracker via workflow_dispatch (cron-job.org)
├── docs/
│   ├── index.html             live index dashboard (GitHub Pages)
│   ├── dashboard.json         index live data written every run
│   ├── stock-dashboard.json   stock live data written every run
│   └── _headers               cache-control: no-cache for dashboard files
├── src/
│   ├── config.py              index instruments, thresholds, R:R, VWAP proximity
│   ├── stock_config.py        stock universe (14 stocks), thresholds, event exclusion config
│   ├── auth.py                Kite TOTP automated login
│   ├── kite_client.py         index OHLCV fetch, live quote, option resolution
│   ├── stock_kite_client.py   stock equity + option token cache
│   ├── indicators.py          VWAP, RSI(14), DMI(14) from scratch
│   ├── signals.py             4-condition CE/PE evaluator (shared by index + stock)
│   ├── state.py               Upstash Redis REST client
│   ├── dashboard_writer.py    JSON write + git commit/push (index)
│   ├── notifier.py            Discord webhook (index signals)
│   ├── stock_events.py        Marketaux corporate event exclusion
│   ├── stock_main.py          stock bot orchestrator
│   ├── journal.py             optional Notion logging
│   ├── calendar_nse.py        trading day + eval window gates
│   ├── main.py                index bot orchestrator
│   ├── position_tracker.py    trailing SL tracker — ladder, AI tightening, heartbeat
│   ├── trade_notifier.py      Discord FYI / ACTION / Exit embeds (tracker-only)
│   └── discord_listener.py    Discord REST polling for /enter and /exit triggers
├── holidays_2026.json
├── requirements.txt
└── verify_setup.py
```

---

## Setup

### 1. Verify prerequisites

```bash
python verify_setup.py
```

### 2. Add GitHub Secrets

Repo Settings → Secrets and variables → Actions:

| Secret | Description |
|---|---|
| `KITE_API_KEY` | Kite Connect app API key |
| `KITE_API_SECRET` | Kite Connect app API secret |
| `KITE_USER_ID` | Zerodha login ID (e.g. IZ3912) |
| `KITE_PASSWORD` | Zerodha login password |
| `KITE_TOTP_SECRET` | Base32 TOTP seed from the 2FA QR code |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST endpoint URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis REST bearer token |
| `DISCORD_WEBHOOK_URL` | Discord webhook for index CE/PE signal alerts |
| `DISCORD_STOCK_WEBHOOK_URL` | Discord webhook for stock CE/PE alerts + event exclusion reports |
| `DISCORD_TRADE_TRACKER_WEBHOOK_URL` | Discord webhook for FYI / ACTION / Exit trade tracker embeds |
| `DISCORD_BOT_TOKEN` | Discord Bot token (read messages) for /enter and /exit detection |
| `TRADE_TRACKER_CHANNEL_ID` | Discord channel ID where /enter and /exit are typed |
| `MARKETAUX_API_TOKEN` | Marketaux free-tier API token (stock event exclusion) |
| `NOTION_TOKEN` | (Optional) Notion integration token |
| `NOTION_DB_ID` | (Optional) Notion signals database ID |

`GITHUB_TOKEN` is auto-provided by Actions for git push — do not add it.

### 3. Enable GitHub Pages

Repo Settings → Pages → Source: **main branch, /docs folder**

### 4. Set up external cron trigger

`signal.yml` uses `workflow_dispatch` only (no native GitHub cron) because GitHub's scheduled workflows can be delayed 10–30 minutes, which is unacceptable for market-hour signal evaluation.

Point cron-job.org (or equivalent) to trigger the workflow every 5 minutes between 09:40–14:45 IST on weekdays. Use the GitHub API dispatch endpoint with a repo-scoped PAT.

### 5. Test manually

1. Trigger `morning-login.yml` via workflow_dispatch
2. Verify `kite:access_token` appears in Upstash console
3. Trigger `signal.yml` during market hours
4. Check Discord for a test signal or watch `docs/dashboard.json` update

---

## Configuration Reference

All tunable parameters are in `src/config.py`:

| Parameter | Value | Meaning |
|---|---|---|
| `EVAL_WINDOW_START` | `09:40` | Earliest time signals can fire (IST) |
| `EVAL_WINDOW_END` | `14:45` | Latest time (IST) |
| `DI_THRESHOLD` | `25` | Minimum DI value for C4 to pass |
| `REQUIRE_DI_DOMINANCE` | `True` | Dominant DI must exceed opposing DI |
| `DI_TREND_CHECK` | `True` | DI must be rising across 3 points |
| `TARGET_RR` | `1.5` | Risk:reward ratio for target calculation |
| `ATM_DELTA` | `0.50` | Delta used to convert spot risk to premium |
| `COOLDOWN_CANDLES` | `3` | Min candles (×5 min = 15 min) between same-direction signals |
| `RSI_SLOPE_CANDLES` | `3` | Number of points for RSI slope check |
| `USE_ADX_FILTER` | `False` | ADX filter (computed but disabled) |

---

## Constraints

- **Never places orders.** Read-only from Kite's perspective. Signal + alert only.
- **Latest closed candle is always `df.iloc[-2]`**, not `df.iloc[-1]` (last candle may be forming).
- **VWAP resets at 09:15 IST.** Prior candles receive `NaN`.
- **RSI and DMI use the full DataFrame** including prior-session warm-up for Wilder accuracy.
- **ATM strike uses `round()`, not `int()`** — they differ for .5 boundary cases.
- **All times in IST** (`ZoneInfo("Asia/Kolkata")`).
- **Git commit only when dashboard.json changes** — a no-signal run never creates an empty commit.
