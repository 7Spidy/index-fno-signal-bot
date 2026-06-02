#!/usr/bin/env python3
"""
verify_setup.py — Pre-flight validation for index-fno-signal-bot.

Run this LOCALLY (not in GitHub Actions) before your first deploy
to confirm every secret and connection is working.

Usage:
    pip install kiteconnect pyotp requests python-dotenv
    cp .env.example .env        # fill in your values
    python verify_setup.py
"""

import os, sys, json, time
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # env vars already set

PASS = "  ✅"
FAIL = "  ❌"
WARN = "  ⚠️ "

errors = []

def check(label, ok, detail=""):
    sym = PASS if ok else FAIL
    print(f"{sym}  {label}" + (f"\n        {detail}" if detail else ""))
    if not ok:
        errors.append(label)
    return ok

print("\n" + "="*60)
print("  index-fno-signal-bot — Pre-flight Check")
print("="*60 + "\n")

# ── 1. Required env vars present ─────────────────────────────
print("── 1. Environment Variables ─────────────────────────────")
required = [
    "KITE_API_KEY", "KITE_API_SECRET",
    "KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET",
    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "DISCORD_WEBHOOK_URL",
]
optional = ["NOTION_TOKEN", "NOTION_DB_ID"]

for k in required:
    v = os.environ.get(k, "")
    check(k, bool(v), "NOT SET — required" if not v else f"set ({len(v)} chars)")

for k in optional:
    v = os.environ.get(k, "")
    sym = PASS if v else WARN
    print(f"{sym}  {k}" + (" — optional, not set" if not v else f" — set ({len(v)} chars)"))

# ── 2. TOTP secret format ────────────────────────────────────
print("\n── 2. TOTP Secret Validation ────────────────────────────")
try:
    import pyotp
    secret = os.environ.get("KITE_TOTP_SECRET", "")
    totp = pyotp.TOTP(secret)
    code = totp.now()
    check("TOTP generates valid 6-digit code", len(code) == 6 and code.isdigit(),
          f"Generated: {code} — verify this matches your authenticator app")
except Exception as e:
    check("TOTP secret valid", False, str(e))

# ── 3. Upstash Redis ─────────────────────────────────────────
print("\n── 3. Upstash Redis Connection ──────────────────────────")
try:
    import requests
    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if url and token:
        # SET a test key
        r = requests.post(f"{url}/set/preflight_test/ok",
                          headers={"Authorization": f"Bearer {token}"}, timeout=5)
        set_ok = r.status_code == 200
        check("Redis SET", set_ok, r.text[:80] if not set_ok else "")

        # GET it back
        r2 = requests.get(f"{url}/get/preflight_test",
                          headers={"Authorization": f"Bearer {token}"}, timeout=5)
        val = r2.json().get("result") if r2.status_code == 200 else None
        check("Redis GET", val == "ok", f"Got: {val}")

        # DELETE
        requests.post(f"{url}/del/preflight_test",
                      headers={"Authorization": f"Bearer {token}"}, timeout=5)
    else:
        check("Upstash credentials present", False, "URL or token missing")
except Exception as e:
    check("Upstash Redis", False, str(e))

# ── 4. Discord webhook ───────────────────────────────────────
print("\n── 4. Discord Webhook ───────────────────────────────────")
try:
    import requests
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook:
        payload = {
            "embeds": [{
                "title": "🔧 Pre-flight Test",
                "description": "verify_setup.py — connection confirmed.",
                "color": 0x00e5a0,
                "footer": {"text": f"Sent at {datetime.now().strftime('%H:%M:%S IST')}"}
            }]
        }
        r = requests.post(webhook, json=payload, timeout=5)
        check("Discord webhook POST", r.status_code in (200, 204),
              f"HTTP {r.status_code}" if r.status_code not in (200, 204) else
              "Check your Discord channel — test message should appear")
    else:
        check("Discord webhook URL present", False, "Not set")
except Exception as e:
    check("Discord webhook", False, str(e))

# ── 5. Kite API key format ───────────────────────────────────
print("\n── 5. Kite Connect API Key ──────────────────────────────")
api_key = os.environ.get("KITE_API_KEY", "")
check("KITE_API_KEY non-empty", bool(api_key))
check("KITE_API_KEY length (expect 16 chars)", len(api_key) == 16,
      f"Got {len(api_key)} chars — check kite.trade developer console")
api_secret = os.environ.get("KITE_API_SECRET", "")
check("KITE_API_SECRET non-empty", bool(api_secret))

# ── 6. docs/ folder structure ───────────────────────────────
print("\n── 6. Repo Structure ────────────────────────────────────")
import pathlib
checks = [
    ("docs/", pathlib.Path("docs").is_dir()),
    ("docs/index.html", pathlib.Path("docs/index.html").exists()),
    ("docs/dashboard.json", pathlib.Path("docs/dashboard.json").exists()),
    ("docs/_headers", pathlib.Path("docs/_headers").exists()),
    ("holidays_2026.json", pathlib.Path("holidays_2026.json").exists()),
    ("src/config.py", pathlib.Path("src/config.py").exists()),
    ("src/main.py", pathlib.Path("src/main.py").exists()),
    ("requirements.txt", pathlib.Path("requirements.txt").exists()),
]
for label, ok in checks:
    check(label, ok, "missing — build not yet complete" if not ok else "")

# ── Summary ──────────────────────────────────────────────────
print("\n" + "="*60)
if errors:
    print(f"  ❌ {len(errors)} issue(s) to fix before deploy:\n")
    for e in errors:
        print(f"     • {e}")
    print()
    sys.exit(1)
else:
    print("  ✅ All checks passed — ready to deploy!\n")
    print("  Next step: trigger morning-login.yml via workflow_dispatch")
    print("  then watch the dashboard at your GitHub Pages URL.\n")
    sys.exit(0)
