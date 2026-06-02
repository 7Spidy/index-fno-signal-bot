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


class _TokenCaptured(Exception):
    def __init__(self, url: str):
        self.url = url


class _InterceptAdapter(requests.adapters.HTTPAdapter):
    """Let Zerodha requests through; intercept everything else.

    When Kite issues a 302 to the app's redirect_url (e.g. http://127.0.0.1),
    requests will call send() on this adapter before making the TCP connection.
    We raise _TokenCaptured with that URL instead of actually connecting,
    so the caller can extract request_token without needing a live server.
    """
    def send(self, request, **kwargs):
        if "zerodha.com" not in request.url:
            raise _TokenCaptured(request.url)
        return super().send(request, **kwargs)


def _extract_request_token(session: requests.Session, login_url: str) -> str:
    import re

    # ── Strategy 1: custom adapter intercepts the redirect to redirect_url ──
    # Mount on both http:// and https:// so we catch any redirect_url scheme.
    # Zerodha URLs pass through to the real connection; everything else is
    # intercepted before a TCP connection is attempted.
    intercept = _InterceptAdapter()
    session.mount("http://", intercept)
    session.mount("https://", intercept)

    try:
        r = session.get(login_url, allow_redirects=True, timeout=20)
        # If we land here, redirect_url was actually reachable (very unlikely)
        if "request_token" in r.url:
            return parse_qs(urlparse(r.url).query)["request_token"][0]
        for h in r.history:
            loc = h.headers.get("Location", "")
            if "request_token" in loc:
                return parse_qs(urlparse(loc).query)["request_token"][0]
        # We landed on connect/finish HTML — fall through to strategy 2
        finish_html = r.text
        finish_url = r.url

    except _TokenCaptured as e:
        # Happy path: intercepted redirect to redirect_url containing request_token
        if "request_token" in e.url:
            return parse_qs(urlparse(e.url).query)["request_token"][0]
        raise RuntimeError(f"Intercepted non-Zerodha redirect but no request_token: {e.url}")

    # ── Strategy 2: connect/finish returned 200 HTML ──
    # Print the first 1500 chars for debugging, then try to handle it.
    print(f"[auth-debug] Landed on HTML page: {finish_url}")
    print(f"[auth-debug] Page preview (first 1500 chars):\n{finish_html[:1500]}")

    # 2a. request_token anywhere in the body
    m = re.search(r'request_token[=:"\s]+([A-Za-z0-9]{20,})', finish_html)
    if m:
        print(f"[auth-debug] Found request_token in HTML body")
        return m.group(1)

    # 2b. JavaScript window.location or meta refresh containing request_token
    m = re.search(r'(?:window\.location|location\.href|content=["\']0;url=)[^"\']*request_token=([A-Za-z0-9]+)', finish_html)
    if m:
        print(f"[auth-debug] Found request_token in JS redirect")
        return m.group(1)

    # 2c. Extract api_key + sess_id from URL and POST directly to /connect/finish
    parsed = urlparse(finish_url)
    url_params = parse_qs(parsed.query)
    api_key_val = url_params.get("api_key", [""])[0]
    sess_id_val = url_params.get("sess_id", [""])[0]

    if api_key_val and sess_id_val:
        print(f"[auth-debug] Trying direct POST to /connect/finish with sess_id={sess_id_val[:8]}...")
        for post_data in [
            {"api_key": api_key_val, "sess_id": sess_id_val, "action": "allow"},
            {"api_key": api_key_val, "sess_id": sess_id_val},
        ]:
            try:
                r2 = session.post(
                    "https://kite.zerodha.com/connect/finish",
                    data=post_data,
                    allow_redirects=False,
                    timeout=15,
                )
                print(f"[auth-debug] POST status={r2.status_code} location={r2.headers.get('Location','')[:120]}")
                loc2 = r2.headers.get("Location", "")
                if "request_token" in loc2:
                    return parse_qs(urlparse(loc2).query)["request_token"][0]
            except _TokenCaptured as e:
                if "request_token" in e.url:
                    return parse_qs(urlparse(e.url).query)["request_token"][0]
            except Exception as ex:
                print(f"[auth-debug] POST attempt failed: {ex}")

    # 2d. Parse and submit any HTML form found
    action_m = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', finish_html, re.IGNORECASE)
    if action_m:
        action = action_m.group(1) or finish_url
        if not action.startswith("http"):
            action = f"https://kite.zerodha.com{action}"
        inputs: dict[str, str] = {}
        for inp in re.finditer(r'<input([^>]*)/?>', finish_html, re.IGNORECASE):
            attrs = inp.group(1)
            n = re.search(r'name=["\']([^"\']+)["\']', attrs)
            v = re.search(r'value=["\']([^"\']*)["\']', attrs)
            if n:
                inputs[n.group(1)] = v.group(1) if v else ""
        print(f"[auth-debug] Submitting form: action={action} inputs={list(inputs.keys())}")
        try:
            r3 = session.post(action, data=inputs, allow_redirects=False, timeout=15)
            loc3 = r3.headers.get("Location", "")
            print(f"[auth-debug] Form POST status={r3.status_code} location={loc3[:120]}")
            if "request_token" in loc3:
                return parse_qs(urlparse(loc3).query)["request_token"][0]
        except _TokenCaptured as e:
            if "request_token" in e.url:
                return parse_qs(urlparse(e.url).query)["request_token"][0]

    raise RuntimeError(
        f"request_token not found. Last URL: {finish_url}\n"
        "See [auth-debug] lines above for the page content.\n"
        "Checklist:\n"
        "  1. KITE_API_KEY matches your kite.trade developer app\n"
        "  2. Kite Connect app redirect URL must be set (e.g. http://127.0.0.1)\n"
        "  3. KITE_TOTP_SECRET must be the base32 seed, not a 6-digit OTP"
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
