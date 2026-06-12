# Spec — Signal-Bot P&L Replay (`tools/pnl_replay.py`)

**Repo:** `7Spidy/index-fno-signal-bot` (Repo 1)
**Status:** FROZEN. Implement exactly. No scope creep.
**Goal:** Replay the *current* signal-bot logic over the last ~2 weeks of 5-min
candles, simulate each trade's exit using the bot's own spot SL/target, and print
a P&L table. Runs fully offline after one data fetch. **No LLM at runtime. No MCP.**

---

## 0. Non-negotiable constraints

1. **Reuse Repo 1 logic — do NOT reimplement.** Import and call the existing
   functions. The tool is a thin replay+exit+P&L layer on top of frozen code:
   - `src.signals.evaluate(df, vwap, rsi, pdi, ndi, cfg) -> dict`
   - `src.indicators.vwap_session(df, session_open)`,
     `src.indicators.rsi_wilder(df)`, `src.indicators.dmi_wilder(df)`
   - `src.config` (`as_dict()`, `INSTRUMENTS`, `TARGET_RR`, `ATM_DELTA`,
     `COOLDOWN_CANDLES`, `EVAL_WINDOW_START/END`)
   - `src.kite_client.get_kite()` for the authenticated `KiteConnect` object
     (reads `kite:access_token` from Upstash — same path the live bot uses).
2. **Data source = `kiteconnect` SDK, NOT the Kite MCP.** The MCP is the
   Claude-facing surface; this tool must run from cron with zero Claude. Get the
   client via `kite_client.get_kite()` and call `kite.historical_data(...)`.
3. **Do NOT modify any existing file.** No edits to `src/`, workflows,
   `docs/`, `dashboard.json`, or tests. The only new files are listed in §9.
4. **No new third-party deps.** Only what Repo 1 already uses
   (`kiteconnect`, `pandas`, `numpy`, `python-dateutil`, stdlib).

---

## 1. Price series & faithfulness decisions (read carefully)

These are deliberate modeling calls. Implement them as written; do not "improve."

- **One series per instrument = near-month FUTURES 5-min candles.** This matches
  the candles the live bot computes indicators on. Resolve the FUT token by
  filtering `kite.instruments(<exch>)` **directly in this tool**: pick
  `instrument_type == "FUT"`, `name == <underlying>`, nearest `expiry >= today`.
  NIFTY/BANKNIFTY on `NFO`, SENSEX on `BFO` (use `src.config.fno_exchange_for(name)`).
  **Do NOT call `src.kite_client.resolve_futures_tokens()`** — it writes
  `kite:instrument_tokens` to Upstash as a side-effect; this backtest must stay
  read-only and touch no Redis state.
- **`reference` (entry price) = futures candle close of the signal candle.**
  Live uses spot LTP with a documented futures fallback; we use futures
  throughout so the SL anchor, reference, and exit-walk are one self-consistent
  series. The fut–spot basis is small; record it as a known approximation in the
  output header. Do NOT fetch spot candles in v1.
- **SL anchor = prior candle extreme**, exactly as live: CE → `prev_candle_low`,
  PE → `prev_candle_high`, where "prior candle" is the candle *before* the signal
  candle (signal candle is `df.iloc[-2]`, prior is `df.iloc[-3]`).
- **Option P&L = delta approximation.** Real historical option premiums are NOT
  fetched (expired weeklies don't resolve in the live instruments dump). Use
  `ATM_DELTA = 0.50`. Premium absolute LTP is unavailable historically and is
  omitted from output (state this in the header). The move-based P&L below does
  not need entry premium.

---

## 2. Per-evaluation warm-up window

For each candidate evaluation candle in session `D` at time `T`:

- Build `work_df` = all loaded candles with `timestamp <= T` **and**
  `timestamp >= start_of_prior_trading_session(D)`. This yields a full prior
  session (~75 candles) of Wilder warm-up plus session `D` up to `T`. This is ≥
  the live bot's warm-up depth, so CE/PE booleans match live in practice.
- `session_open` passed to `vwap_session` = session `D` at **09:15 IST**
  (VWAP resets daily).
- Compute on `work_df`:
  `vwap = vwap_session(work_df, session_open)`,
  `rsi = rsi_wilder(work_df)`,
  `pdi, ndi, adx = dmi_wilder(work_df)`.
- `cfg = src.config.as_dict()`, then set `cfg["strike_step"]` and
  `cfg["instrument_name"]` for the instrument (same as `main.py`).
- `result = signals.evaluate(work_df, vwap, rsi, pdi, ndi, cfg)`.

The "latest closed candle" inside `evaluate` is `work_df.iloc[-2]`. Therefore an
evaluation candle at index `i` in the full series is evaluated by passing
`work_df` whose **last row is `i+1`** (so that `iloc[-2] == i`). Implement the
loop so each real candle `i` is evaluated once as the closed candle. Equivalent
and simpler: iterate `i` over candles, set `work_df = full.loc[window_start : i+1]`
(inclusive of the still-forming next candle), and read the signal off `iloc[-2]`.

---

## 3. Signal gating (replicate live exactly)

A signal at candle `i` (instrument, direction in {CE,PE}) is **taken** iff:

1. `result[dir]["signal"]` is True.
2. Signal candle time (the `iloc[-2]` timestamp) is within the eval window
   **09:40–14:45 IST inclusive** (`EVAL_WINDOW_START/END`). No entries after 14:45.
3. **Cooldown:** fewer than `COOLDOWN_CANDLES` (3) five-minute candles have
   elapsed since the last *taken* same-direction signal **for that instrument**.
   Track last-taken candle-ts per `(instrument, direction)`; suppress if
   `(this_candle_ts - last_taken_ts) < 3 * 5min`. (Mirror `_within_cooldown`.)
4. **Dedup:** one fire per candle per direction — automatic since each candle is
   evaluated once.

No single-position constraint. This is the *signal bot's* efficacy: take every
signal it would have emitted. Overlapping trades across instruments/directions
are independent. (Same instrument+direction can't overlap due to cooldown.)

---

## 4. Level math (computed IN THIS TOOL — `evaluate()` does NOT return levels)

`src.signals.evaluate()` returns the CE/PE booleans plus `prev_candle_high`,
`prev_candle_low`, `candle_high`, `candle_low`, `futures_price`, `atm_strike`,
`rsi`, `pdi`, `ndi`, `vwap` — but **not** `spot_sl`/`spot_tgt`/`raw_risk`. The
tool computes those here, copying `main.py` §4–§7 semantics verbatim.

Let `ref = signal_candle_close`, `rr = config.TARGET_RR (1.5)`,
`min_risk = INSTRUMENTS[inst]["min_risk"]`, `delta = config.ATM_DELTA (0.50)`.

```
if CE:
    spot_sl  = round(prev_candle_low, 1)
    raw_risk = max(ref - spot_sl, min_risk)
    spot_tgt = round(ref + rr * raw_risk, 1)
else:  # PE
    spot_sl  = round(prev_candle_high, 1)
    raw_risk = max(spot_sl - ref, min_risk)
    spot_tgt = round(ref - rr * raw_risk, 1)

atm_strike     = round(ref / strike_step) * strike_step   # Python round()
premium_risk   = raw_risk * delta                          # per-unit, ₹
premium_target = raw_risk * rr * delta                     # per-unit, ₹
```

`prev_candle_low`/`prev_candle_high` come from `result` (the evaluate output
already exposes them) — do not recompute from candles.

Conviction label (display only, from `main.py`):
`spread = (pdi-ndi)` for CE else `(ndi-pdi)`; `>=18 Strong`, `>=10 Moderate`,
else `Building`.

---

## 5. Exit simulation (the new part)

Walk the **same futures series forward**, starting at the candle immediately
after the signal candle (entry is at signal-candle close). For each forward
candle until end of that session:

- **CE:** SL hit if `candle.low <= spot_sl`; target hit if `candle.high >= spot_tgt`.
- **PE:** SL hit if `candle.high >= spot_sl`; target hit if `candle.low <= spot_tgt`.
- **Intrabar tie-break (pessimistic):** if a single candle's range touches both
  SL and target, record **SL** (assume the stop fills first).
- **Hard square-off:** if neither level is hit by the candle whose time is
  **15:10 IST** (or the last candle ≤ 15:10 in that session), exit at that
  candle's **close** (TIME exit).

Exit outcome per trade:

| outcome | exit price | pnl_pts (favorable move) |
|---|---|---|
| TARGET | `spot_tgt` | `+rr * raw_risk` (= +1.5R) |
| SL | `spot_sl` | `-raw_risk` (= −1R) |
| TIME | exit close | CE: `exit - ref`; PE: `ref - exit` (signed) |

```
r_multiple = pnl_pts / raw_risk
pnl_rupees_per_lot = pnl_pts * delta * lot_size      # 1 lot, delta-approx
```

`lot_size` is fetched per instrument from the instruments dump (current value);
do not hardcode. The whole trade lives within one session (intraday system,
15:10 square-off), so no multi-day walk.

---

## 6. Output

Print to stdout AND write a CSV to `./pnl_out/` (create dir).

**(a) Header block** — window dates, instruments, and the explicit caveats:
"futures series used as reference; option P&L = delta(0.50) approximation;
historical option LTP not fetched (expired contracts unresolvable)."

**(b) Per-signal trade table** — one row per taken signal, columns:
`date | time_IST | instr | dir | atm_strike | entry(ref) | spot_sl | spot_tgt |
risk_pts | conviction | outcome | exit | pnl_pts | R | pnl_₹/lot`.
Use a clean monospace table (e.g. `tabulate`-free manual formatting or simple
f-string columns — no new deps).

**(c) Per-signal "Discord-style" alert text** — render the same fields the live
embed shows, as plain text, per taken signal (instrument, direction, ATM strike,
entry/SL/target spot levels, risk_pts, conviction). **Omit live option LTP**
(unavailable historically) and say so once. Save these to
`./pnl_out/alerts.txt`. Add a `--discord` flag (default **off**) that, only when
passed, POSTs each via `src.notifier` — default must never hit Discord.

**(d) Summary** — totals + per-instrument breakdown:
total signals, #TARGET / #SL / #TIME, win rate (TARGET / total),
sum R, sum ₹/lot, and the same split per instrument.

CSV path: `./pnl_out/pnl_<from>_<to>.csv` with the §6b columns.

---

## 7. CLI

```
python -m tools.pnl_replay [--days 14] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                           [--instruments NIFTY,SENSEX] [--discord]
```

- Default: `--days 14` ending today (IST). `--from/--to` override.
- `--instruments` defaults to all three from `config.INSTRUMENTS`.
- Run from repo root so `src.*` imports resolve: `python -m tools.pnl_replay`.
- One `kite.historical_data(token, from_dt, to_dt, "5minute")` call **per
  instrument** for the full window (5-min interval supports the range in a single
  call). Cache nothing to disk beyond the CSV/alerts; re-fetch on each run.

---

## 8. Data fetch details

- Get client: `kite = src.kite_client.get_kite()`. If it raises (no token in
  Redis), print a clear message pointing at the morning-login workflow and exit 1.
- **Do NOT use `src.kite_client.fetch_ohlcv`** — it is hardcoded to a ~5-day,
  `to_date=now` window and cannot pull a 14-day range ending at an arbitrary
  date. Call **`kite.historical_data(token, from_dt, to_dt, "5minute")`**
  directly instead.
- Resolve near-month FUT token **and** `lot_size` per instrument in one pass over
  `kite.instruments(exch)` (`exch = config.fno_exchange_for(name)`): filter
  `instrument_type=="FUT"`, `name==<underlying>`, take min `expiry>=today`; read
  `instrument_token` and `lot_size` off that row.
- `historical_data` returns dicts with `date, open, high, low, close, volume`.
  Build a DataFrame with a `timestamp` column (tz-aware IST) sorted ascending,
  matching the column names `vwap_session`/`rsi_wilder`/`dmi_wilder` expect
  (`high, low, close, volume, timestamp`).
- Skip non-trading days implicitly (Kite returns no candles). No holiday file
  needed; if `holidays_2026.json` is trivially available, you may use it to label
  sessions, but it is optional.

---

## 9. Files (commit discipline)

**Create ONLY these new files:**
- `tools/__init__.py` (empty)
- `tools/pnl_replay.py`
- `pnl-backtest-spec.md` (this spec, added to repo root for provenance)

**Touch nothing else.** `pnl_out/` is a runtime output dir — add `pnl_out/` to
`.gitignore` (append one line; if `.gitignore` doesn't exist, create it with that
one line). The `.gitignore` change is the only edit to a pre-existing file and
must be shown in the diff.

---

## 10. Acceptance checks (run before any commit)

1. `python -m tools.pnl_replay --days 14` runs end-to-end, no exceptions, prints
   header + trade table + summary, writes CSV + `alerts.txt`.
2. No Discord POST occurs without `--discord`.
3. `git status` shows only: `tools/__init__.py`, `tools/pnl_replay.py`,
   `pnl-backtest-spec.md`, and the one-line `.gitignore` change. Nothing under
   `src/`, `docs/`, `.github/`, or `dashboard.json` is modified.
4. Spot-check one TARGET and one SL row by hand against the candle data.
