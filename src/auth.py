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

    # Step 3: follow redirects manually — stop as soon as request_token appears.
    # allow_redirects=True would try to actually connect to the app's redirect
    # URL (e.g. http://127.0.0.1) which isn't running in CI, causing a failure.
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    request_token = _extract_request_token(session, login_url)

    # Step 4: generate session
    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]


def _extract_request_token(session: requests.Session, url: str) -> str:
    """Follow redirects one step at a time, return request_token the moment it appears.
    Never actually connects to the final redirect URL (which is the app's callback
    and won't be running in GitHub Actions)."""
    for _ in range(10):  # max 10 hops
        r = session.get(url, allow_redirects=False)
        # Check current URL first (in case token is already here)
        params = parse_qs(urlparse(url).query)
        if "request_token" in params:
            return params["request_token"][0]
        # Check the redirect Location header
        location = r.headers.get("Location", "")
        if "request_token" in location:
            return parse_qs(urlparse(location).query)["request_token"][0]
        if not location or r.status_code not in (301, 302, 303, 307, 308):
            break
        url = location
    raise RuntimeError(
        f"request_token not found after following redirects. "
        f"Last URL: {url} — check that KITE_API_KEY matches your Kite Connect app."
    )


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
