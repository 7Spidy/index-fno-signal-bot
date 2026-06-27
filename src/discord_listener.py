"""Discord REST API polling for /enter and /exit plain-text triggers."""
import os

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DISCORD_API = "https://discord.com/api/v10"


def fetch_new_messages(
    channel_id: str,
    bot_token: str,
    after_id: str | None,
) -> list[dict]:
    """GET messages from Discord channel after a given message ID.

    Returns list of message dicts (id, content, timestamp), oldest-first.
    On first-ever run (after_id=None), fetches only the 5 most recent messages
    so we don't process a huge backlog.
    """
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}"}

    if after_id:
        params = {"after": after_id, "limit": 100}
    else:
        params = {"limit": 5}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        messages = resp.json()
        # Discord returns newest-first; reverse to oldest-first for processing order
        return list(reversed(messages))
    except Exception as e:
        print(f"[discord_listener] fetch_new_messages failed: {e}")
        return []


def extract_commands(messages: list[dict]) -> list[tuple[str, str]]:
    """Returns [(message_id, command)] for messages that are exactly /enter or /exit."""
    results = []
    for msg in messages:
        content = msg.get("content", "").strip().lower()
        if content in ("/enter", "/exit"):
            results.append((msg["id"], content))
    return results
