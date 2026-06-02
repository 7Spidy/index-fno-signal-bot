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


def _extract_request_token(session: requests.Session, login_url: str) -> str:
    """Follow redirects and handle the connect/finish consent page.

    Kite's connect/finish returns 200 HTML with an authorization form that
    must be submitted (simulates clicking the Allow button). After submission
    Kite issues a 302 to the app's redirect_url with request_token.
    We capture that token from the Location header without connecting to the
    redirect_url itself (which won't be running in GitHub Actions).
    """
    import re

    url = login_url
    for _ in range(15):
        # Check current URL for token (shouldn't happen but be safe)
        if "request_token" in url:
            return parse_qs(urlparse(url).query)["request_token"][0]

        r = session.get(url, allow_redirects=False, timeout=15)

        # Token in redirect Location header — normal happy path
        location = r.headers.get("Location", "")
        if "request_token" in location:
            return parse_qs(urlparse(location).query)["request_token"][0]

        # Follow HTTP redirect
        if r.status_code in (301, 302, 303, 307, 308) and location:
            url = location if location.startswith("http") else f"https://kite.zerodha.com{location}"
            continue

        # 200 HTML — this is the connect/finish consent page.
        # Simulate clicking "Allow" by parsing and submitting the form.
        if r.status_code == 200 and r.text:
            # Occasionally request_token is embedded directly in the HTML
            m = re.search(r'request_token[="\s:]+([A-Za-z0-9]+)', r.text)
            if m:
                return m.group(1)

            # Find the form action
            action_m = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', r.text, re.IGNORECASE)
            action = (action_m.group(1) if action_m else "") or url
            if not action.startswith("http"):
                action = f"https://kite.zerodha.com{action}"

            # Collect all hidden/submit input values
            inputs: dict[str, str] = {}
            for inp in re.finditer(r'<input([^>]*)/?>', r.text, re.IGNORECASE):
                attrs = inp.group(1)
                name_m = re.search(r'name=["\']([^"\']+)["\']', attrs)
                val_m = re.search(r'value=["\']([^"\']*)["\']', attrs)
                if name_m:
                    inputs[name_m.group(1)] = val_m.group(1) if val_m else ""

            r2 = session.post(action, data=inputs, allow_redirects=False, timeout=15)
            loc2 = r2.headers.get("Location", "")
            if "request_token" in loc2:
                return parse_qs(urlparse(loc2).query)["request_token"][0]
            if loc2:
                url = loc2 if loc2.startswith("http") else f"https://kite.zerodha.com{loc2}"
                continue

        break

    raise RuntimeError(
        f"request_token not found after following redirects. Last URL: {url}\n"
        "Checklist:\n"
        "  1. KITE_API_KEY matches your kite.trade developer app\n"
        "  2. KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET are correct\n"
        "  3. Your Kite Connect app's redirect URL is set (any URL works, e.g. http://127.0.0.1)"
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
