"""Optional Notion integration — append signal rows to a Notion database."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers() -> dict:
    token = os.environ.get("NOTION_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def log_signal(instrument: str, direction: str, result: dict) -> bool:
    """Append a signal row to the Notion signals database. Returns True on success."""
    db_id = os.environ.get("NOTION_DB_ID")
    if not db_id:
        return False

    now = datetime.now(IST)
    vwap_val = result.get("vwap") or 0.0
    price = result.get("futures_price") or 0.0
    vwap_delta = round(price - vwap_val, 2)

    is_ce = direction.upper() == "CE"
    conds = result.get("ce" if is_ce else "pe", {})
    cond_text = ", ".join(
        k.upper() for k, v in conds.items() if k.startswith("c") and v
    )

    props = {
        "Date": {"date": {"start": now.date().isoformat()}},
        "Time (IST)": {"rich_text": [{"text": {"content": now.strftime("%H:%M")}}]},
        "Instrument": {"title": [{"text": {"content": instrument}}]},
        "Direction": {"select": {"name": direction.upper()}},
        "Futures Price": {"number": price},
        "ATM Strike": {"number": result.get("atm_strike") or 0},
        "RSI": {"number": round(result.get("rsi") or 0, 2)},
        "+DI": {"number": round(result.get("pdi") or 0, 2)},
        "-DI": {"number": round(result.get("mdi") or 0, 2)},
        "VWAP Delta": {"number": vwap_delta},
        "Conditions": {"rich_text": [{"text": {"content": cond_text}}]},
        "Signal": {"checkbox": True},
    }

    try:
        r = requests.post(
            f"{NOTION_API}/pages",
            headers=_headers(),
            json={"parent": {"database_id": db_id}, "properties": props},
            timeout=15,
        )
        r.raise_for_status()
        print(f"[journal] ✓ Logged {instrument} {direction} to Notion")
        return True
    except Exception as e:
        print(f"[journal] Notion log failed: {e}")
        return False
