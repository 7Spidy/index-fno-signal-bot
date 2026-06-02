"""Upstash Redis via REST API. No redis-py — pure HTTPS calls."""
import os
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_BASE_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


def redis_get(key: str) -> str | None:
    try:
        r = requests.get(f"{_BASE_URL}/get/{key}", headers=_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("result")
    except Exception as e:
        print(f"[state] redis_get({key!r}) error: {e}")
        return None


def redis_set(key: str, value: str, ex: int | None = None) -> bool:
    try:
        url = f"{_BASE_URL}/set/{key}/{value}"
        if ex is not None:
            url += f"?ex={ex}"
        r = requests.post(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("result") == "OK"
    except Exception as e:
        print(f"[state] redis_set({key!r}) error: {e}")
        return False


def redis_delete(key: str) -> bool:
    try:
        r = requests.post(f"{_BASE_URL}/del/{key}", headers=_headers(), timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[state] redis_delete({key!r}) error: {e}")
        return False


def redis_exists(key: str) -> bool:
    try:
        r = requests.get(f"{_BASE_URL}/exists/{key}", headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("result", 0) == 1
    except Exception as e:
        print(f"[state] redis_exists({key!r}) error: {e}")
        return False


if __name__ == "__main__":
    print("Testing Upstash Redis connection...")
    ok = redis_set("test:ping", "pong", ex=60)
    print(f"  set: {ok}")
    val = redis_get("test:ping")
    print(f"  get: {val}")
    exists = redis_exists("test:ping")
    print(f"  exists: {exists}")
    deleted = redis_delete("test:ping")
    print(f"  delete: {deleted}")
    gone = redis_exists("test:ping")
    print(f"  exists after delete: {gone}")
    if ok and val == "pong" and exists and deleted and not gone:
        print("  ✓ All Redis operations succeeded")
    else:
        print("  ✗ One or more operations failed — check UPSTASH env vars")
