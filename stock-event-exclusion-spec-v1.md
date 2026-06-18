# Spec — Stock Event Exclusion (`src/stock_events.py`)

**Repo:** `7Spidy/index-fno-signal-bot` (Repo 1)
**Status:** FROZEN v1. Implement exactly. No scope creep.
**Goal:** Skip any of the 7 tracked stocks from evaluation for the day if it has
an earnings, dividend, or corporate-action event (split / bonus / AGM) in the
next 3 calendar days. Alert-only bot — this affects evaluation and alerting,
nothing downstream in Repo 2 (executor is NIFTY-only and untouched by this).

---

## 0. Non-negotiable constraints

1. **Fail open, never closed.** If the NSE scrape fails — partially or
   completely — the exclusion list for that day must default to fewer
   exclusions, never more. A bad NSE morning must degrade to "checked nothing,
   trading normally," not "blocked everything." Silence is the wrong failure
   mode for a once-a-day, low-blast-radius gate like this.
2. **The new morning-login step must never fail the job.** `stock_events.py`
   catches everything internally and always exits 0. The YAML step also gets
   `continue-on-error: true` as a second layer — the Kite login step earlier in
   that workflow is the one that actually matters and must never be blocked by
   this.
3. **No new third-party dependencies.** `requests` is already a dependency
   (`state.py`, `notifier.py`). Implement the session/cookie/retry handling
   inline — no `fake-useragent`, no scraping framework.
4. **Touch nothing in Repo 2 / the executor.** This is a Repo 1, alert-only
   concern. NIFTY isn't a tracked stock so this has zero interaction with the
   executor's risk gate or anything in `executor/config.py`.
5. **One NSE call per symbol, not two.** `GET /api/top-corp-info` bundles
   corporate actions and board meetings in one response — see §2. Don't add a
   second endpoint call; it doubles WAF exposure for no benefit.

---

## 1. Decisions (confirmed)

| Question | Decision |
|---|---|
| Event types | Earnings + dividend ex-date + corporate actions (split / bonus / AGM) |
| Data source | Auto-scrape NSE's corporate-data endpoint, once daily |
| What "excluded" means | Fully skip — no evaluation, no Discord alert, no dashboard card |
| Window | 3 calendar days, inclusive of today (today + next 2 days) |
| Board-meeting matching | Any board meeting in the window counts — no purpose-text filtering |
| Failure handling | Fail open + tiered Discord warning (see §5) |

---

## 2. Data source — verified against a live, maintained NSE wrapper

Endpoint (confirmed live in `hi-imcodeman/stock-nse-india`, not assumed from
training data):

```
GET https://www.nseindia.com/api/top-corp-info?symbol={SYMBOL}&market=equities
```

Relevant response shape:

```json
{
  "corporate_actions": {
    "data": [
      {"symbol": "RELIANCE", "exdate": "21-Jun-2026", "purpose": "Dividend - Rs 5.50 Per Share"}
    ]
  },
  "borad_meeting": {
    "data": [
      {"symbol": "RELIANCE", "purpose": "To consider financial results", "meetingdate": "19-Jun-2026"}
    ]
  }
}
```

`borad_meeting` (sic) is NSE's actual field name — typo included, verified from
the wrapper's TypeScript interface. Do not "fix" the spelling when reading it.

**Date format is the one unverified detail in this spec.** The wrapper types
both `exdate` and `meetingdate` as plain `string` with no documented format.
Historically NSE corporate-data fields use `DD-Mon-YYYY` (e.g. `21-Jun-2026`).
Implement parsing defensively (try `%d-%b-%Y` first; on `ValueError`, log the
raw string and skip that row rather than crashing the symbol's check) and
confirm the real format on the first live dry run before trusting it blindly.

### Session / headers

NSE sits behind an Akamai WAF that can 403 even a properly-cookied session —
documented directly in the wrapper's source comments, not a guess. Mitigate
with a standard browser-like session, not a bypass:

```python
_HEADERS = {
    "Authority": "www.nseindia.com",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nseindia.com",
    "Referer": "https://www.nseindia.com/",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
```

Flow: `GET https://www.nseindia.com/` first (cookie handshake, discard body) →
reuse the session's cookie jar for the `top-corp-info` calls. On a 401/403,
reset the session (new `requests.Session()`, re-handshake) and retry **once**
per symbol — don't loop indefinitely. A static User-Agent is a deliberate
simplification vs. the wrapper's UA rotation (constraint §0.3); if blocks
persist in practice, UA rotation is the first thing to revisit, not a second
endpoint or a proxy.

### Why one endpoint, not two

`top-corp-info` already contains everything needed for all three event types
in §1 — corporate actions for dividend/bonus/split, board meetings for
earnings/AGM. Two endpoints × 7 symbols = 14 NSE round-trips for the same data
one endpoint gives in 7. Halving the call count halves WAF exposure.

---

## 3. Matching rules

**Corporate actions** (`corporate_actions.data[]`): keep a row if its lowercased
`purpose` contains `"dividend"`, `"bonus"`, or `"split"`, **and** its `exdate`
falls in the window (§4).

**Board meetings** (`borad_meeting.data[]`): keep a row if its `meetingdate`
falls in the window. No `purpose` filtering — per §1, any meeting counts. This
is deliberately over-inclusive: a false positive costs one skipped stock for a
day; a false negative trades straight through a results announcement. AGM is
the weakest signal of the three event types (rarely moves price the way
results/dividends do) but it's included because §1 says so.

A symbol is excluded if **either** check matches.

---

## 4. Window

`EVENT_LOOKAHEAD_DAYS = 3` — calendar days, inclusive of today:
`today <= event_date <= today + 2 days`. Calendar days, not trading days —
corporate dates are calendar dates by nature (a Saturday ex-date is still a
real ex-date). SEBI requires board meetings to be intimated at least 2 working
days ahead, so a 3-day-ahead window run at ~09:05 IST should reliably have
already-published intimations for anything happening in that window.

---

## 5. Failure handling

Track per-symbol success/failure while building the exclusion list.

- **All 7 symbols failed** (total NSE block): exclusion list = `[]`. Send one
  Discord warning naming this explicitly and telling the operator to verify
  upcoming results/dividends manually that morning.
- **Some symbols failed** (partial): exclusion list = whatever succeeded.
  Send one Discord warning naming the failure count, flagging the list as
  possibly incomplete.
- **Zero failures**: no warning, regardless of whether anything was excluded.

Reuse `notifier.send_warning(message)` — same channel/severity tier as the
existing `⚠️ STOCK BOT: No access token` warning. Do not build a new
Discord-posting function for this.

---

## 6. Redis schema

```
key:   stock:event_excluded:{YYYY-MM-DD}        (today, IST)
value: JSON list of excluded symbol strings, e.g. ["RELIANCE", "INFY"]
ttl:   64800   # 18 hours — covers one trading day with margin
```

Date-scoped so a missed write on one day can never leak into the next.
`stock_main.py` reads this key **once per 5-minute run** (not per stock) and
checks membership in a Python `set` inside the per-stock loop.

---

## 7. New file — `src/stock_events.py`

```python
"""
stock_events.py — NSE corporate-event exclusion list for the stock bot.

Scrapes NSE's top-corp-info endpoint for the 7 tracked stocks once a day
(morning-login), and writes the set of symbols with an earnings / dividend /
split / bonus / AGM event in the next EVENT_LOOKAHEAD_DAYS calendar days to
Redis. Fails open: any scrape failure results in an empty (or partial)
exclusion list plus a Discord warning, never a full block of the stock bot.

Called from morning-login.yml as:
    python -m src.stock_events --cache-event-exclusions
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta

import requests

from src import notifier, state
from src import stock_config as cfg

_BASE = "https://www.nseindia.com"
_HEADERS = { ... }   # see §2


def _session() -> requests.Session:
    """Cookie handshake against the homepage, returns a ready session."""


def _in_window(d: date, today: date) -> bool:
    return today <= d <= today + timedelta(days=cfg.EVENT_LOOKAHEAD_DAYS - 1)


def _parse_nse_date(raw: str) -> date | None:
    """Defensive parse — see §2. Returns None (and logs) on failure."""


def _has_event(session: requests.Session, symbol: str, today: date) -> bool:
    """
    One GET to /api/top-corp-info for `symbol`. Use requests' params={...}
    dict, NOT an f-string URL — M&M's "&" must be percent-encoded or the
    query string silently breaks. Retries once on 401/403 with a fresh
    session. Returns True if either corporate_actions or borad_meeting has a
    matching row in the window (§3). Raises on anything else — caller's
    per-symbol try/except turns that into a counted failure.
    """


def compute_excluded() -> tuple[list[str], int]:
    """Returns (excluded_symbols, failure_count) — one pass over cfg.STOCKS,
    each symbol wrapped in its own try/except so one bad symbol never kills
    the other 6."""


def cache_event_exclusions() -> None:
    """Entry point. Writes the Redis key (§6) and sends the tiered Discord
    warning (§5). Always exits 0 — never raises past this function."""


if __name__ == "__main__":
    if "--cache-event-exclusions" in sys.argv:
        cache_event_exclusions()
```

---

## 8. Changes to existing files

### `src/stock_config.py` — append

```python
# Event exclusion — stocks with earnings/dividend/corp-action events in the
# next N calendar days (inclusive of today) are skipped entirely by stock_main.
EVENT_LOOKAHEAD_DAYS = 3
REDIS_EVENT_EXCLUDED_PREFIX = "stock:event_excluded"   # + ":{YYYY-MM-DD}"
```

### `src/stock_main.py` — `main()`, right after `equity_tokens` is loaded

```python
equity_tokens: dict[str, int] = json.loads(raw_equity)

# Event exclusion — skip stocks with an upcoming earnings/dividend/corp-action
# event. Written once daily by morning-login's stock_events.py step; a
# missing key just means "nothing excluded today."
excluded_key   = f"{cfg.REDIS_EVENT_EXCLUDED_PREFIX}:{date.today().isoformat()}"
raw_excluded   = state.redis_get(excluded_key)
event_excluded: set[str] = set(json.loads(raw_excluded)) if raw_excluded else set()

now                = datetime.now(IST)
```

### `src/stock_main.py` — top of the per-stock loop

```python
for stock in cfg.STOCKS:
    name = stock["name"]

    if name in event_excluded:
        print(f"[stock_main] {name}: skipped — event within "
              f"{cfg.EVENT_LOOKAHEAD_DAYS}d (earnings/dividend/corp action)")
        continue

    try:
        token_id = equity_tokens.get(name)
        ...
```

### `.github/workflows/morning-login.yml` — new step, after "Cache stock option tokens (monthly)"

```yaml
      - name: Cache stock event exclusions
        run: python -m src.stock_events --cache-event-exclusions
        continue-on-error: true
        env:
          UPSTASH_REDIS_REST_URL:   ${{ secrets.UPSTASH_REDIS_REST_URL }}
          UPSTASH_REDIS_REST_TOKEN: ${{ secrets.UPSTASH_REDIS_REST_TOKEN }}
          DISCORD_WEBHOOK_URL:      ${{ secrets.DISCORD_WEBHOOK_URL }}
```

No `KITE_*` secrets — this step never touches Kite.

---

## 9. Files (commit discipline)

**Create:**
- `src/stock_events.py`
- `stock-event-exclusion-spec-v1.md` (this file, added to repo root for provenance)

**Modify (only these, only the additions shown in §8):**
- `src/stock_config.py`
- `src/stock_main.py`
- `.github/workflows/morning-login.yml`

**Touch nothing else.** No changes to `src/config.py`, anything under
`executor/`, `docs/`, or any test file.

---

## 10. Acceptance checks (run before any commit)

1. `python -m src.stock_events --cache-event-exclusions` run standalone exits 0
   and prints the excluded list (or `none`), against live NSE.
2. Confirm the Redis key `stock:event_excluded:{today}` exists afterward with
   the expected TTL (~64800s) and JSON shape.
3. Manually set the Redis key to a JSON list containing one real stock name,
   run `stock_main.py` once, and confirm in logs that the stock was skipped
   and does **not** appear in `docs/stock-dashboard.json`'s `instruments`.
4. Temporarily point `_BASE` at an invalid host (or block it) to simulate an
   NSE outage; confirm `cache_event_exclusions()` still exits 0, writes an
   empty list, and posts exactly one Discord warning naming all-7 failure.
5. `git status` shows only the files in §9. Nothing under `executor/` is
   touched.
6. Confirm `M&M` round-trips correctly through the NSE call (the `&` is the
   one symbol in `cfg.STOCKS` that can break naive URL construction).

---

## 11. Deferred / open items

- Exact `exdate` / `meetingdate` string format — confirm on first live dry run
  (§2). If it differs from `%d-%b-%Y`, fix the parser; this is the only piece
  of this spec not yet verified against a real NSE response body.
- AGM-specific filtering (separating it from earnings within `borad_meeting`)
  — not needed for v1 per the over-inclusive default in §3, but could be added
  later if AGM-only exclusions turn out to be unnecessarily conservative.
- User-Agent rotation if NSE blocks prove persistent in practice (§2).
