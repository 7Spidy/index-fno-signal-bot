"""Kite Connect TOTP automated login."""
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests
import pyotp
from kiteconnect import KiteConnect

from src import state

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")


def get_access_token(api_key: str, api_secret: str, user_id: str,
                     password: str, totp_secret: str) -> str:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: password login
    r1 = session.post("https://kite.zerodha.com/api/login",
                      data={"user_id": user_id, "password": password})
    r1.raise_for_status()
    request_id = r1.json()["data"]["request_id"]

    # Step 2: TOTP 2FA
    totp_code = pyotp.TOTP(totp_secret).now()
    r2 = session.post("https://kite.zerodha.com/api/twofa",
                      data={"user_id": user_id, "request_id": request_id,
                            "twofa_value": totp_code, "twofa_type": "totp"})
    r2.raise_for_status()

    # Step 3: follow login URL to get request_token from redirect
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    r3 = session.get(login_url, allow_redirects=True)
    params = parse_qs(urlparse(r3.url).query)
    request_token = params["request_token"][0]

    # Step 4: generate session
    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]


def run_morning_login() -> None:
    api_key = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    user_id = os.environ["KITE_USER_ID"]
    password = os.environ["KITE_PASSWORD"]
    totp_secret = os.environ["KITE_TOTP_SECRET"]

    try:
        print(f"[auth] Logging in as {user_id}...")
        token = get_access_token(api_key, api_secret, user_id, password, totp_secret)
        now_ist = datetime.now(IST).isoformat()

        state.redis_set("kite:access_token", token, ex=43200)
        state.redis_set("kite:token_refreshed_at", now_ist)
        print(f"[auth] ✓ Token stored in Redis at {now_ist}")

    except Exception as e:
        print(f"[auth] ✗ Login failed: {e}")
        _send_login_failure_alert(str(e))
        sys.exit(1)


def _send_login_failure_alert(error_msg: str) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return
    payload = {
        "embeds": [{
            "title": "⚠️ Kite Login Failed",
            "description": f"Morning login failed. Manual intervention required.\n\n```{error_msg}```",
            "color": 0xf59e0b,
            "footer": {"text": "index-fno-signal-bot · morning-login"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    run_morning_login()
