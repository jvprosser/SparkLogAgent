"""Git commit of the installed package, for connection checks."""

from __future__ import annotations

import json
import os

# Set SPARK_LOGS_GIT_VERSION when starting the utility. Nothing runs git.
GIT_VERSION = os.environ.get("SPARK_LOGS_GIT_VERSION", "unknown")


def git_version() -> str:
    return _commit_from_install() or os.environ.get("SPARK_LOGS_GIT_VERSION", GIT_VERSION)


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
