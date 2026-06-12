"""Signal-Bot P&L Replay — offline backtest of the frozen signal logic.

Implements `pnl-backtest-spec.md` exactly. Replays the last ~2 weeks of
near-month FUTURES 5-min candles through the repo's frozen signal logic
(src.signals / src.indicators), takes every signal the live bot would emit
(eval-window + 3-candle cooldown gates), simulates each exit against the bot's
own spot SL/target, and prints a P&L table + per-instrument summary, writes a
CSV, and saves plain-text Discord-style alerts.

Runs fully offline after one historical_data fetch per instrument. No LLM, no
MCP. Default run NEVER posts to Discord — only --discord enables it.

Usage (from repo root):
    python -m tools.pnl_replay [--days 14] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                               [--instruments NIFTY,SENSEX] [--discord]
"""
import argparse
import csv
import os
import sys
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from src import config, indicators, signals
from src import kite_client

IST = ZoneInfo("Asia/Kolkata")

# Max risk per instrument (points). Mirrors main.py §2 gate; defined inline
# here because src.config carries this as live-bot logic, not a config constant.
MAX_RISK_POINTS = {"NIFTY": 20, "BANKNIFTY": 100, "SENSEX": 80}

SQUAREOFF_TIME = dtime(15, 10)   # hard intraday square-off (§5)
CANDLE_SECONDS = 5 * 60


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def resolve_fut(kite, name: str):
    """Resolve near-month FUT instrument_token + lot_size for `name`.

    Filters the live instruments dump directly (§1/§8): instrument_type=='FUT',
    name==underlying, nearest expiry>=today. Read-only — does NOT touch Redis.
    """
    exch = config.fno_exchange_for(name)
    instruments = kite.instruments(exch)
    today = datetime.now(IST).date()
    futs = [i for i in instruments
            if i["name"] == name
            and i["instrument_type"] == "FUT"
            and i["expiry"] >= today]
    if not futs:
        return None
    nearest = min(futs, key=lambda x: x["expiry"])
    return {
        "token":         nearest["instrument_token"],
        "tradingsymbol": nearest["tradingsymbol"],
        "expiry":        nearest["expiry"],
        "lot_size":      nearest["lot_size"],
        "exchange":      exch,
    }


def fetch_candles(kite, token: int, from_dt: datetime, to_dt: datetime) -> pd.DataFrame:
    """One historical_data call for the full window (§8). Returns a DataFrame
    with tz-aware IST `timestamp` and OHLCV columns, sorted ascending."""
    data = kite.historical_data(
        instrument_token=token,
        from_date=from_dt,
        to_date=to_dt,
        interval="5minute",
        continuous=False,
        oi=False,
    )
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.rename(columns={"date": "timestamp"})
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    df["timestamp"] = ts
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def conviction_label(pdi: float, ndi: float, direction: str) -> str:
    spread = (pdi - ndi) if direction == "CE" else (ndi - pdi)
    if spread >= 18:
        return "Strong"
    if spread >= 10:
        return "Moderate"
    return "Building"


def simulate_exit(forward: pd.DataFrame, direction: str,
                  spot_sl: float, spot_tgt: float, ref: float):
    """Walk forward candles (same session, after the signal candle) until SL/
    target/square-off. Returns (outcome, exit_price). §5 semantics:
    intrabar SL-first tie-break; 15:10 hard square-off at that candle's close.
    """
    squareoff = forward[forward["timestamp"].dt.time <= SQUAREOFF_TIME]
    for _, c in squareoff.iterrows():
        if direction == "CE":
            sl_hit = c["low"] <= spot_sl
            tg_hit = c["high"] >= spot_tgt
        else:  # PE
            sl_hit = c["high"] >= spot_sl
            tg_hit = c["low"] <= spot_tgt
        if sl_hit:                       # SL-first tie-break (pessimistic)
            return "SL", spot_sl
        if tg_hit:
            return "TARGET", spot_tgt
    # No level hit by square-off → TIME exit at last candle's close ≤ 15:10
    if len(squareoff) > 0:
        return "TIME", float(squareoff.iloc[-1]["close"])
    return "TIME", ref                   # degenerate: no candle before 15:10


# --------------------------------------------------------------------------- #
# Core replay
# --------------------------------------------------------------------------- #
def replay_instrument(kite, name: str, strike_step: int, min_risk: int,
                      from_dt: datetime, to_dt: datetime,
                      ew_start: dtime, ew_end: dtime, rr: float,
                      delta: float, cooldown_candles: int,
                      max_risk_pts: int = 9999) -> list[dict]:
    """Replay one instrument over [from_dt, to_dt]. Returns a list of taken-trade
    dicts (already exit-simulated)."""
    fut = resolve_fut(kite, name)
    if not fut:
        print(f"[pnl_replay] {name}: no near-month FUT found — skipping")
        return []
    print(f"[pnl_replay] {name}: FUT {fut['tradingsymbol']} "
          f"(token={fut['token']}, lot={fut['lot_size']})")

    full = fetch_candles(kite, fut["token"], from_dt, to_dt)
    if full.empty:
        print(f"[pnl_replay] {name}: no candles returned — skipping")
        return []
    print(f"[pnl_replay] {name}: {len(full)} candles "
          f"({full['timestamp'].iloc[0].date()} → {full['timestamp'].iloc[-1].date()})")

    full["session_date"] = full["timestamp"].dt.date
    sessions = sorted(full["session_date"].unique())
    first_pos = {d: int(full.index[full["session_date"] == d][0]) for d in sessions}

    cfg = config.as_dict()
    cfg["strike_step"] = strike_step
    cfg["instrument_name"] = name

    last_taken: dict[tuple, datetime] = {}
    trades: list[dict] = []

    # Skip the first loaded session: it has no prior session for Wilder warm-up (§2).
    for s in range(1, len(sessions)):
        D = sessions[s]
        prior = sessions[s - 1]
        window_start_pos = first_pos[prior]
        session_open = datetime.combine(D, dtime(9, 15), tzinfo=IST)

        sess_positions = list(full.index[full["session_date"] == D])
        for i in sess_positions:
            # Need a still-forming successor candle in the SAME session so that
            # work_df.iloc[-2] == candle i (the closed candle). (§2)
            if i + 1 >= len(full) or full.at[i + 1, "session_date"] != D:
                continue

            work_df = full.iloc[window_start_pos:i + 2].reset_index(drop=True)

            vwap = indicators.vwap_session(work_df, session_open)
            rsi = indicators.rsi_wilder(work_df)
            pdi, ndi, adx = indicators.dmi_wilder(work_df)
            result = signals.evaluate(work_df, vwap, rsi, pdi, ndi, cfg)

            for direction in ("ce", "pe"):
                if not result[direction]["signal"]:
                    continue

                dir_up = direction.upper()
                candle_ts = full.at[i, "timestamp"]
                ts_time = candle_ts.time()

                # Gate 2: eval window 09:40–14:45 IST inclusive (§3.2)
                if ts_time < ew_start or ts_time > ew_end:
                    continue

                # Gate 3: cooldown per (instrument, direction) (§3.3)
                key = (name, dir_up)
                prev_ts = last_taken.get(key)
                if prev_ts is not None and \
                        (candle_ts - prev_ts).total_seconds() < cooldown_candles * CANDLE_SECONDS:
                    continue

                # ── Level math (§4) — futures close is the reference ──
                ref = float(result["futures_price"])
                if dir_up == "CE":
                    spot_sl = round(float(result["prev_candle_low"]), 1)
                    raw_risk = max(ref - spot_sl, min_risk)
                    spot_tgt = round(ref + rr * raw_risk, 1)
                else:
                    spot_sl = round(float(result["prev_candle_high"]), 1)
                    raw_risk = max(spot_sl - ref, min_risk)
                    spot_tgt = round(ref - rr * raw_risk, 1)

                # Gate 5: max-risk filter — mirrors main.py §2 exactly.
                # Suppresses signals where the prev-candle structural SL is too
                # far from entry (wide candle = oversized risk).
                if raw_risk > max_risk_pts:
                    print(f"[pnl_replay] SKIPPED {name} {dir_up} "
                          f"@ {candle_ts.strftime('%H:%M')}: "
                          f"risk {raw_risk:.1f} pts > max {max_risk_pts} pts "
                          f"(wide prev candle)")
                    continue

                atm_strike = round(ref / strike_step) * strike_step
                conviction = conviction_label(
                    float(result["pdi"]), float(result["ndi"]), dir_up)

                # ── Exit simulation (§5) ──
                forward = full.iloc[i + 1:][full.iloc[i + 1:]["session_date"] == D]
                outcome, exit_price = simulate_exit(
                    forward, dir_up, spot_sl, spot_tgt, ref)

                if outcome == "TARGET":
                    pnl_pts = rr * raw_risk
                elif outcome == "SL":
                    pnl_pts = -raw_risk
                else:  # TIME
                    pnl_pts = (exit_price - ref) if dir_up == "CE" else (ref - exit_price)

                r_multiple = pnl_pts / raw_risk if raw_risk else 0.0
                pnl_rupees = pnl_pts * delta * fut["lot_size"]

                last_taken[key] = candle_ts
                trades.append({
                    "date":        D.isoformat(),
                    "time_IST":    candle_ts.strftime("%H:%M"),
                    "instr":       name,
                    "dir":         dir_up,
                    "atm_strike":  int(atm_strike),
                    "entry":       round(ref, 2),
                    "spot_sl":     round(spot_sl, 1),
                    "spot_tgt":    round(spot_tgt, 1),
                    "risk_pts":    round(raw_risk, 1),
                    "conviction":  conviction,
                    "outcome":     outcome,
                    "exit":        round(float(exit_price), 2),
                    "pnl_pts":     round(pnl_pts, 1),
                    "R":           round(r_multiple, 2),
                    "pnl_rupees":  round(pnl_rupees, 0),
                    # carried for alerts / --discord rendering
                    "_rsi":        result.get("rsi"),
                    "_pdi":        result.get("pdi"),
                    "_ndi":        result.get("ndi"),
                    "_vwap":       result.get("vwap"),
                    "_candle_time": result.get("candle_time"),
                    "_lot_size":   fut["lot_size"],
                    "_rr":         rr,
                    "_c": {k: result[direction][k] for k in ("c1", "c2", "c3", "c4")},
                })

    return trades


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
CAVEATS = (
    "futures series used as reference (entry/SL/target/exit-walk all on the "
    "near-month FUT 5-min series; fut–spot basis is a known small approximation);\n"
    "  option P&L = delta(0.50) approximation;\n"
    "  historical option LTP not fetched (expired contracts unresolvable) — "
    "absolute premium levels omitted."
)

TABLE_COLS = [
    ("date",       "date",       10),
    ("time_IST",   "time",        5),
    ("instr",      "instr",       9),
    ("dir",        "dir",         3),
    ("atm_strike", "atm",         7),
    ("entry",      "entry",      10),
    ("spot_sl",    "spot_sl",    10),
    ("spot_tgt",   "spot_tgt",   10),
    ("risk_pts",   "risk",        7),
    ("conviction", "conviction", 10),
    ("outcome",    "outcome",     7),
    ("exit",       "exit",       10),
    ("pnl_pts",    "pnl_pts",     8),
    ("R",          "R",           6),
    ("pnl_rupees", "pnl_₹/lot",  11),
]

CSV_FIELDS = ["date", "time_IST", "instr", "dir", "atm_strike", "entry",
              "spot_sl", "spot_tgt", "risk_pts", "conviction", "outcome",
              "exit", "pnl_pts", "R", "pnl_rupees"]


def _fmt_cell(val) -> str:
    if isinstance(val, float):
        return f"{val:g}"
    return str(val)


def print_header(from_date: date, to_date: date, names: list[str]) -> None:
    print("=" * 96)
    print("  SIGNAL-BOT P&L REPLAY  (offline backtest)")
    print("=" * 96)
    print(f"  Window      : {from_date.isoformat()} → {to_date.isoformat()}  (IST)")
    print(f"  Instruments : {', '.join(names)}")
    print(f"  Series      : near-month FUTURES, 5-minute candles")
    print(f"  Gates       : eval window {config.EVAL_WINDOW_START}–{config.EVAL_WINDOW_END} IST, "
          f"cooldown {config.COOLDOWN_CANDLES} candles, R:R 1:{config.TARGET_RR}")
    print(f"  Caveats     : {CAVEATS}")
    print("=" * 96)


def print_table(trades: list[dict]) -> None:
    header = "  ".join(f"{title:<{w}}" for _, title, w in TABLE_COLS)
    print(header)
    print("-" * len(header))
    for t in trades:
        row = "  ".join(f"{_fmt_cell(t[key]):<{w}}" for key, _, w in TABLE_COLS)
        print(row)


def print_summary(trades: list[dict], names: list[str]) -> None:
    print()
    print("=" * 96)
    print("  SUMMARY")
    print("=" * 96)

    def block(label: str, rows: list[dict]) -> None:
        n = len(rows)
        if n == 0:
            print(f"  {label:<12}: no signals")
            return
        n_tgt = sum(1 for r in rows if r["outcome"] == "TARGET")
        n_sl = sum(1 for r in rows if r["outcome"] == "SL")
        n_time = sum(1 for r in rows if r["outcome"] == "TIME")
        win = 100.0 * n_tgt / n
        sum_r = sum(r["R"] for r in rows)
        sum_rs = sum(r["pnl_rupees"] for r in rows)
        print(f"  {label:<12}: {n:>3} signals  |  "
              f"TARGET {n_tgt:>2}  SL {n_sl:>2}  TIME {n_time:>2}  |  "
              f"win {win:5.1f}%  |  ΣR {sum_r:+7.2f}  |  Σ₹/lot {sum_rs:+,.0f}")

    block("ALL", trades)
    print("-" * 96)
    for name in names:
        block(name, [t for t in trades if t["instr"] == name])
    print("=" * 96)


def write_csv(trades: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for t in trades:
            w.writerow({k: t[k] for k in CSV_FIELDS})


def alert_text(t: dict) -> str:
    arrow = "↑" if t["dir"] == "CE" else "↓"
    return (
        f"{'🟢' if t['dir'] == 'CE' else '🔴'} {t['dir']} Signal — {t['instr']}\n"
        f"  Candle      : {t['date']} {t['time_IST']} IST\n"
        f"  ATM strike  : {t['atm_strike']} {t['dir']}\n"
        f"  Entry (ref) : {t['entry']}  (futures close)\n"
        f"  Spot SL     : {t['spot_sl']}\n"
        f"  Spot Target : {t['spot_tgt']}\n"
        f"  Risk (pts)  : {t['risk_pts']}  ·  R:R 1:{t['_rr']}\n"
        f"  Conviction  : {t['conviction']}\n"
        f"  RSI {arrow}       : {t['_rsi']:.1f}\n"
        f"  +DI / -DI   : {t['_pdi']:.1f} / {t['_ndi']:.1f}\n"
        f"  [backtest exit: {t['outcome']} @ {t['exit']}  ·  "
        f"{t['pnl_pts']:+} pts  ·  {t['R']:+}R]\n"
    )


def write_alerts(trades: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("Discord-style alert text (offline backtest)\n")
        f.write("NOTE: live option LTP / premium levels are omitted — historical "
                "option premiums are not fetched (expired contracts unresolvable).\n")
        f.write("=" * 72 + "\n\n")
        for t in trades:
            f.write(alert_text(t))
            f.write("\n")


def post_discord(trades: list[dict]) -> None:
    """Only invoked when --discord is passed. Builds a result-like dict per trade
    and posts via src.notifier.send_signal (option premium fields are None →
    notifier renders them as 'unavailable')."""
    from src import notifier
    for t in trades:
        result = {
            "atm_data":        {"strike": t["atm_strike"]},
            "atm_ltp":         None,
            "opt_target":      None,
            "opt_sl":          None,
            "spot_ltp":        None,
            "spot_tgt":        t["spot_tgt"],
            "spot_sl":         t["spot_sl"],
            "fut_spot_spread": None,
            "futures_price":   t["entry"],
            "vwap":            t["_vwap"],
            "conviction":      t["conviction"],
            "rr":              t["_rr"],
            "candle_time":     t["_candle_time"],
            "rsi":             t["_rsi"],
            "pdi":             t["_pdi"],
            "ndi":             t["_ndi"],
            "c1": t["_c"]["c1"], "c2": t["_c"]["c2"],
            "c3": t["_c"]["c3"], "c4": t["_c"]["c4"],
        }
        notifier.send_signal(t["instr"], t["dir"], result)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tools.pnl_replay",
        description="Offline P&L replay of the frozen signal-bot logic.")
    ap.add_argument("--days", type=int, default=14,
                    help="Lookback window in days, ending today IST (default 14).")
    ap.add_argument("--from", dest="from_date", default=None,
                    help="Start date YYYY-MM-DD (overrides --days).")
    ap.add_argument("--to", dest="to_date", default=None,
                    help="End date YYYY-MM-DD (default today IST).")
    ap.add_argument("--instruments", default=None,
                    help="Comma list, e.g. NIFTY,SENSEX (default all in config).")
    ap.add_argument("--discord", action="store_true",
                    help="POST each alert via src.notifier (default OFF).")
    ap.add_argument("--max-risk-override", default=None, dest="max_risk_override",
                    metavar="INSTR:PTS[,...]",
                    help="Override MAX_RISK_POINTS for one or more instruments, "
                         "e.g. NIFTY:40,BANKNIFTY:150,SENSEX:120.")
    args = ap.parse_args(argv)

    # Console may be cp1252 on Windows; the table/summary use ₹ Σ → arrows.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    all_names = [i["name"] for i in config.INSTRUMENTS]
    if args.instruments:
        requested = [s.strip().upper() for s in args.instruments.split(",") if s.strip()]
        unknown = [n for n in requested if n not in all_names]
        if unknown:
            print(f"[pnl_replay] Unknown instrument(s): {', '.join(unknown)}. "
                  f"Known: {', '.join(all_names)}")
            return 1
        names = requested
    else:
        names = all_names

    today = datetime.now(IST).date()
    to_date = date.fromisoformat(args.to_date) if args.to_date else today
    from_date = (date.fromisoformat(args.from_date) if args.from_date
                 else to_date - timedelta(days=args.days))

    from_dt = datetime.combine(from_date, dtime(0, 0), tzinfo=IST)
    to_dt = datetime.combine(to_date, dtime(23, 59, 59), tzinfo=IST)

    # Apply --max-risk-override before replay (unknown instruments are an error).
    max_risk = dict(MAX_RISK_POINTS)
    if args.max_risk_override:
        for token in args.max_risk_override.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" not in token:
                print(f"[pnl_replay] Bad --max-risk-override token (expected INSTR:PTS): {token!r}")
                return 1
            instr, pts = token.split(":", 1)
            instr = instr.strip().upper()
            if instr not in max_risk:
                print(f"[pnl_replay] --max-risk-override: unknown instrument {instr!r}. "
                      f"Known: {', '.join(max_risk)}")
                return 1
            try:
                max_risk[instr] = int(pts)
            except ValueError:
                print(f"[pnl_replay] --max-risk-override: pts must be an integer, got {pts!r}")
                return 1
        print(f"[pnl_replay] max_risk overrides applied: "
              f"{', '.join(f'{k}:{v}' for k, v in max_risk.items())}")

    ew_start = _parse_hhmm(config.EVAL_WINDOW_START)
    ew_end = _parse_hhmm(config.EVAL_WINDOW_END)
    rr = config.TARGET_RR
    delta = config.ATM_DELTA
    cooldown = config.COOLDOWN_CANDLES
    min_risk_by_name = {i["name"]: i["min_risk"] for i in config.INSTRUMENTS}
    step_by_name = {i["name"]: i["strike_step"] for i in config.INSTRUMENTS}

    # Auth — fail clearly if no token (§8)
    try:
        kite = kite_client.get_kite()
    except Exception as e:
        print(f"[pnl_replay] Could not get authenticated Kite client: {e}")
        print("[pnl_replay] Run the morning-login workflow (morning-login.yml) "
              "to populate kite:access_token in Upstash, then retry.")
        return 1

    print_header(from_date, to_date, names)

    all_trades: list[dict] = []
    for name in names:
        try:
            trades = replay_instrument(
                kite, name, step_by_name[name], min_risk_by_name[name],
                from_dt, to_dt, ew_start, ew_end, rr, delta, cooldown,
                max_risk.get(name, 9999))
            all_trades.extend(trades)
        except Exception as e:
            print(f"[pnl_replay] ERROR replaying {name}: {e}")

    all_trades.sort(key=lambda t: (t["date"], t["time_IST"], t["instr"]))

    out_dir = os.path.join(os.getcwd(), "pnl_out")
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"pnl_{from_date.isoformat()}_{to_date.isoformat()}.csv")
    alerts_path = os.path.join(out_dir, "alerts.txt")

    print()
    if all_trades:
        print_table(all_trades)
    else:
        print("  (no signals taken in this window)")
    print_summary(all_trades, names)

    write_csv(all_trades, csv_path)
    write_alerts(all_trades, alerts_path)
    print()
    print(f"[pnl_replay] CSV    → {csv_path}")
    print(f"[pnl_replay] Alerts → {alerts_path}")

    if args.discord:
        print("[pnl_replay] --discord set: posting alerts via src.notifier ...")
        post_discord(all_trades)
    else:
        print("[pnl_replay] (no Discord POST — default; pass --discord to enable)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
