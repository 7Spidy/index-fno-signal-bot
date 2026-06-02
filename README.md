# Index F&O Signal Bot

GitHub Actions + Kite Connect signal bot for NIFTY, BANKNIFTY, FINNIFTY, and MIDCPNIFTY futures.

Runs every 5 minutes during NSE market hours (09:40–14:45 IST). Evaluates 4 CE and 4 PE conditions per instrument using session VWAP, RSI(14), and DMI(14). Sends Discord alerts only when all 4 conditions fire. Never places orders.

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
| `KITE_USER_ID` | Zerodha login ID |
| `KITE_PASSWORD` | Zerodha login password |
| `KITE_TOTP_SECRET` | Base32 TOTP seed from 2FA setup QR |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis REST token |
| `DISCORD_WEBHOOK_URL` | Discord channel webhook URL |
| `NOTION_TOKEN` | (Optional) Notion integration token |
| `NOTION_DB_ID` | (Optional) Notion signals database ID |

### 3. Enable GitHub Pages

Repo Settings → Pages → Source: **main branch, /docs folder**

### 4. Test manually

Trigger `morning-login.yml` via workflow_dispatch. Verify `kite:access_token` appears in Upstash console. Then trigger `signal.yml` during market hours and watch the live dashboard.

## Architecture

- `morning-login.yml` — daily ~09:05 IST: Kite TOTP login → Redis token + instrument cache + dashboard reset
- `signal.yml` — every 5min 09:40–14:45 IST: fetch OHLCV → indicators → signals → Discord on fire → dashboard update
- `docs/index.html` — GitHub Pages live dashboard, polls `dashboard.json` every 5 minutes

## Signal Logic

All 4 conditions must pass (CE and PE are directional mirrors):

| # | CE (bullish) | PE (bearish) |
|---|---|---|
| C1 | Close above prior close | Close below prior close |
| C2 | VWAP cross-up within 30min | VWAP cross-down within 30min |
| C3 | RSI rising 3 candles | RSI falling 3 candles |
| C4 | +DI > 25 and dominant | -DI > 25 and dominant |

See `fno-signal-bot-spec-v3.html` for full specification including indicator math.
