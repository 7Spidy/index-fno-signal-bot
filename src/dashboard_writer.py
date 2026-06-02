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
MAX_HISTORY = 280  # 70 ticks × 4 instruments


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

    data["active_signals"] = [
        {
            "instrument": r["name"],
            "direction": d,
            "atm_strike": r.get("atm_strike"),
        }
        for r in instruments_results
        for d in (
            (["CE"] if r["ce"]["signal"] else []) +
            (["PE"] if r["pe"]["signal"] else [])
        )
    ]

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
            "mdi": r.get("mdi"),
            "price": r.get("futures_price"),
        }
        for r in instruments_results
    ]
    data["history"] = (new_rows + data["history"])[:MAX_HISTORY]

    DOCS.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[dashboard] Written {FILE} at {now.strftime('%H:%M IST')}")
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
        reset_day()
    else:
        print("[dashboard] Use --reset-day or call update_and_commit() from main.py")
