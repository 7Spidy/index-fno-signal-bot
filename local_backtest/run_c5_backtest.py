"""
run_c5_backtest.py

Offline backtest runner for local_backtest/c5_liquidity_sweep.py.

Reads cached 5-minute OHLC candle JSON (Kite historical_data response
format — a list of {"date", "open", "high", "low", "close", ...} dicts)
per instrument, groups candles into trading days, scans each day for
liquidity-sweep signals against the prior day's PDH/PDL, simulates a
simple SL/target walk-forward exit, and reports aggregate stats.

Per test_c5_liquidity_sweep_PLAN.md §3/§5: minimum 90-120 trading days
needed before drawing directional conclusions, and 100+ signals per
instrument before promotion is even considered. This is a prototype-only
validation run — no automatic promotion into src/signals.py.

Usage:
    python -m local_backtest.run_c5_backtest --data-dir DIR \
        --from 2026-05-01 --to 2026-06-30
"""
import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List

from local_backtest.c5_liquidity_sweep import (
    Candle,
    SweepSignal,
    compute_entry_sl_target,
    scan_for_sweeps,
)

INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX"]
MIN_SIGNALS_FOR_CONCLUSIONS = 100


def load_candles(path: Path) -> List[Candle]:
    raw = json.loads(path.read_text())
    candles = [
        Candle(
            ts=datetime.fromisoformat(row["date"]),
            open=row["open"], high=row["high"], low=row["low"], close=row["close"],
        )
        for row in raw
    ]
    candles.sort(key=lambda c: c.ts)
    return candles


def group_by_day(candles: List[Candle]) -> dict[date, List[Candle]]:
    days: dict[date, List[Candle]] = {}
    for c in candles:
        days.setdefault(c.ts.date(), []).append(c)
    return days


@dataclass
class TradeResult:
    signal: SweepSignal
    day: date
    exit_reason: str  # "target" | "sl" | "eod"
    r_realized: float


def simulate_trade(signal: SweepSignal, day_candles: List[Candle], entry_buffer_pct: float) -> TradeResult:
    levels = compute_entry_sl_target(signal, entry_buffer_pct=entry_buffer_pct)
    entry, sl, target, risk = levels["entry"], levels["sl"], levels["target"], levels["risk_pts"]

    for c in day_candles[signal.confirm_candle_idx + 1:]:
        if signal.direction == "long":
            if c.low <= sl:
                return TradeResult(signal, day_candles[0].ts.date(), "sl", (sl - entry) / risk)
            if c.high >= target:
                return TradeResult(signal, day_candles[0].ts.date(), "target", (target - entry) / risk)
        else:
            if c.high >= sl:
                return TradeResult(signal, day_candles[0].ts.date(), "sl", (entry - sl) / risk)
            if c.low <= target:
                return TradeResult(signal, day_candles[0].ts.date(), "target", (entry - target) / risk)

    last_close = day_candles[-1].close
    if signal.direction == "long":
        r = (last_close - entry) / risk
    else:
        r = (entry - last_close) / risk
    return TradeResult(signal, day_candles[0].ts.date(), "eod", r)


def max_drawdown(r_sequence: List[float]) -> float:
    cum = 0.0
    peak = 0.0
    worst = 0.0
    for r in r_sequence:
        cum += r
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def run(data_dir: Path, from_date: date, to_date: date, entry_buffer_pct: float):
    report = {}
    for name in INSTRUMENTS:
        path = data_dir / f"{name}.json"
        if not path.exists():
            print(f"[skip] no data file for {name}: {path}")
            continue

        days = group_by_day(load_candles(path))
        trading_days = sorted(days.keys())

        trades: List[TradeResult] = []
        for i in range(1, len(trading_days)):
            d = trading_days[i]
            if not (from_date <= d <= to_date):
                continue
            prior_day_candles = days[trading_days[i - 1]]
            day_candles = days[d]
            sigs = scan_for_sweeps(day_candles, prior_day_candles, name)
            for sig in sigs:
                trades.append(simulate_trade(sig, day_candles, entry_buffer_pct))

        r_values = [t.r_realized for t in trades]
        wins = [r for r in r_values if r > 0]
        n = len(trades)
        report[name] = {
            "signal_count": n,
            "win_rate": (len(wins) / n) if n else None,
            "avg_r": (sum(r_values) / n) if n else None,
            "max_drawdown_r": max_drawdown(r_values) if n else None,
            "meets_100_signal_threshold": n >= MIN_SIGNALS_FOR_CONCLUSIONS,
            "exit_breakdown": {
                reason: sum(1 for t in trades if t.exit_reason == reason)
                for reason in ("target", "sl", "eod")
            },
        }
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--from", dest="from_date", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="to_date", required=True, type=date.fromisoformat)
    ap.add_argument("--entry-buffer-pct", type=float, default=0.01)
    args = ap.parse_args()

    report = run(args.data_dir, args.from_date, args.to_date, args.entry_buffer_pct)

    print(f"\nC5 Liquidity Sweep backtest — {args.from_date} to {args.to_date}\n")
    for name, stats in report.items():
        print(f"=== {name} ===")
        if stats["signal_count"] == 0:
            print("  no signals\n")
            continue
        print(f"  signals:        {stats['signal_count']}")
        print(f"  win rate:       {stats['win_rate']:.1%}")
        print(f"  avg R:          {stats['avg_r']:.3f}")
        print(f"  max drawdown:   {stats['max_drawdown_r']:.2f} R")
        print(f"  exits:          {stats['exit_breakdown']}")
        if not stats["meets_100_signal_threshold"]:
            print("  *** below 100-signal threshold — DO NOT draw directional conclusions ***")
        print()


if __name__ == "__main__":
    main()
