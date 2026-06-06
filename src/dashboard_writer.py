"""Load, update, and git-commit docs/dashboard.json."""
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")
DOCS = Path("docs")
FILE = DOCS / "dashboard.json"
MAX_HISTORY = 140  # 70 ticks × 2 instruments


def load() -> dict:
    if FILE.exists():
        data = json.loads(FILE.read_text(encoding="utf-8"))
        if data.get("date") != date.today().isoformat():
            data["history"] = []
            data["date"] = date.today().isoformat()
        return data
    return {
        "date": date.today().isoformat(),
        "last_run": None,
        "token_valid": False,
        "token_refreshed_at": None,
        "instruments": [],
        "active_signals": [],
        "history": [],
    }


def update_and_commit(instruments_results: list, token_refreshed_at: str | None = None) -> None:
    from src import state as _state
    if token_refreshed_at is None:
        token_refreshed_at = _state.redis_get("kite:token_refreshed_at")

    data = load()
    now = datetime.now(IST)
    data["last_run"] = now.isoformat()
    data["token_valid"] = True
    data["token_refreshed_at"] = token_refreshed_at
    data["instruments"] = instruments_results

    active = []
    for r in instruments_results:
        for d in (["CE"] * int(bool(r["ce"]["signal"])) +
                  ["PE"] * int(bool(r["pe"]["signal"]))):
            active.append(_build_signal_entry(r["name"], d, r))
    data["active_signals"] = active

    new_rows = [
        {
            "time": now.strftime("%H:%M"),
            "instrument": r["name"],
            "ce_conditions": [r["ce"][k] for k in ["c1", "c2", "c3", "c4"]],
            "pe_conditions": [r["pe"][k] for k in ["c1", "c2", "c3", "c4"]],
            "ce_signal": r["ce"]["signal"],
            "pe_signal": r["pe"]["signal"],
            "rsi": r.get("rsi"),
            "pdi": r.get("pdi"),
            "ndi": r.get("ndi"),
            "price": r.get("futures_price"),
        }
        for r in instruments_results
    ]
    data["history"] = (new_rows + data["history"])[:MAX_HISTORY]

    DOCS.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[dashboard] Written {FILE} at {now.strftime('%H:%M IST')}")
    _git_commit(now)


def _build_signal_entry(instrument: str, direction: str, result: dict) -> dict:
    return {
        "instrument":      instrument,
        "direction":       direction,
        "candle_time":     result.get("candle_time"),
        "futures_price":   result.get("futures_price"),
        "spot_ltp":        result.get("spot_ltp"),
        "fut_spot_spread": result.get("fut_spot_spread"),
        "tradingsymbol":   result.get("atm_data", {}).get("tradingsymbol"),
        "strike":          result.get("atm_data", {}).get("strike"),
        "expiry":          result.get("atm_data", {}).get("expiry"),
        "fetch_time":      result.get("atm_data", {}).get("fetch_time"),
        "atm_ltp":         result.get("atm_ltp"),
        "opt_target":      result.get("opt_target"),
        "opt_sl":          result.get("opt_sl"),
        "spot_tgt":        result.get("spot_tgt"),
        "spot_sl":         result.get("spot_sl"),
        "raw_risk":        result.get("raw_risk"),
        "conviction":      result.get("conviction"),
        "rr":              result.get("rr"),
        "rsi":             result.get("rsi"),
        "pdi":             result.get("pdi"),
        "ndi":             result.get("ndi"),
        "vwap":            result.get("vwap"),
        "c1":              result.get("c1"),
        "c2":              result.get("c2"),
        "c3":              result.get("c3"),
        "c4":              result.get("c4"),
    }


def update_active_signal(instrument: str, direction: str, result: dict) -> None:
    """Immediately write one rich signal entry to active_signals and push."""
    data = load()
    entry = _build_signal_entry(instrument, direction, result)
    data["active_signals"] = [
        s for s in data["active_signals"]
        if not (s.get("instrument") == instrument and s.get("direction") == direction)
    ]
    data["active_signals"].append(entry)
    now = datetime.now(IST)
    DOCS.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[dashboard] Active signal written: {instrument} {direction}")
    _git_commit(now)


def reset_day() -> None:
    data = load()
    now = datetime.now(IST)
    data["history"] = []
    data["date"] = date.today().isoformat()
    data["active_signals"] = []
    data["token_valid"] = True
    data["token_refreshed_at"] = now.isoformat()

    DOCS.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[dashboard] Day reset at {now.isoformat()}")
    _git_commit(now)


def _git_commit(now: datetime) -> None:
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], check=True)
        subprocess.run(["git", "add", "docs/dashboard.json"], check=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode != 0:
            msg = f"dashboard: {now.strftime('%H:%M IST')}"
            subprocess.run(["git", "commit", "-m", msg], check=True)
            subprocess.run(["git", "push"], check=True)
            print(f"[dashboard] ✓ Committed and pushed: {msg}")
        else:
            print("[dashboard] No changes to commit")
    except subprocess.CalledProcessError as e:
        print(f"[dashboard] Git error: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset-day", action="store_true")
    args = parser.parse_args()
    if args.reset_day:
        now_ist = datetime.now(IST)
        if now_ist.hour < 10:
            reset_day()
        else:
            print(
                f"[dashboard] reset_day() skipped — already {now_ist.strftime('%H:%M IST')}. "
                "Only resets before 10:00 IST to protect intraday history."
            )
    else:
        print("[dashboard] Use --reset-day or call update_and_commit() from main.py")
