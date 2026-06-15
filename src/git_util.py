"""Rebase-safe git commit and push helper."""
from __future__ import annotations

import subprocess
import time


def commit_and_push(paths: list[str], message: str, *, retries: int = 3) -> bool:
    """add → commit (skip if no staged diff) → push, retrying through
    `git pull --rebase --autostash` on non-fast-forward. Never raises."""
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], check=True)
        subprocess.run(["git", "add"] + paths, check=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode == 0:
            print(f"[git_util] Nothing to commit for: {paths}")
            return True
        subprocess.run(["git", "commit", "-m", message], check=True)
    except subprocess.CalledProcessError as e:
        print(f"[git_util] add/commit failed: {e}")
        return False

    for attempt in range(retries):
        try:
            subprocess.run(["git", "push"], check=True)
            print(f"[git_util] ✓ Pushed: {message}")
            return True
        except subprocess.CalledProcessError:
            print(f"[git_util] push failed (attempt {attempt + 1}/{retries}) — rebasing")
            try:
                subprocess.run(["git", "pull", "--rebase", "--autostash"], check=True)
            except subprocess.CalledProcessError:
                print(f"[git_util] rebase failed — aborting")
                subprocess.run(["git", "rebase", "--abort"])
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    print(f"[git_util] ✗ Exhausted {retries} push attempts for: {message}")
    return False
