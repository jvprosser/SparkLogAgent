"""Git commit of the installed package, for connection checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def git_version() -> str:
    commit = _commit_from_install()
    if commit:
        return commit
    root = _repository_root()
    if root is None:
        return "unknown"
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not commit:
        return "unknown"
    return commit


def _commit_from_install() -> str | None:
    try:
        from importlib.metadata import distribution

        text = distribution("sparklogagent").read_text("direct_url.json")
    except Exception:
        return None
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    vcs = parsed.get("vcs_info") if isinstance(parsed, dict) else None
    if isinstance(vcs, dict) and isinstance(vcs.get("commit_id"), str) and vcs["commit_id"]:
        return vcs["commit_id"]
    return None


def _repository_root() -> Path | None:
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for candidate in [start, *start.parents]:
            if (candidate / ".git").exists():
                return candidate
    return None
