"""Request handling for the yarn logs utility."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from typing import Any

from log_utility.auth import AuthError
from log_utility.version import git_version
from log_utility.yarn import YarnError, YarnRunner, YarnTimeout

logger = logging.getLogger("sparklogagent.utility")

APPLICATION_ID = re.compile(r"^application_[0-9]+_[0-9]+$")
CONTAINER_ID = re.compile(r"^container_(e[0-9]+_)?[0-9]+_[0-9]+_[0-9]+_[0-9]+$")
PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@/-]{0,127}$")
LOG_TYPES = {"stdout", "stderr"}
MAX_BODY = 16384
MAX_TAIL_LINES = 2000


class LogService:
    def __init__(
        self,
        token: str,
        authenticator: Any,
        yarn: YarnRunner,
        max_bytes: int = 65536,
        tail_lines: int = 200,
        concurrency: int = 4,
        acquire_timeout: float = 5,
    ) -> None:
        self.token = token
        self.authenticator = authenticator
        self.yarn = yarn
        self.max_bytes = max_bytes
        self.tail_lines = tail_lines
        self.acquire_timeout = acquire_timeout
        self._slots = threading.BoundedSemaphore(concurrency)

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path.split("?", 1)[0] == "/status":
            if not _bearer_ok(headers.get("Authorization", ""), self.token):
                return 401, {"error": "unauthorized"}
            return 200, {"status": "ok", "git_version": git_version()}
        if method != "POST":
            return 405, {"error": "method not allowed"}
        if not _bearer_ok(headers.get("Authorization", ""), self.token):
            return 401, {"error": "unauthorized"}
        route, application_id = _route(path.split("?", 1)[0])
        if route is None or application_id is None:
            return 404, {"error": "not found"}
        if len(body) > MAX_BODY:
            return 413, {"error": "request too large"}
        try:
            payload = json.loads(body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "invalid json"}
        if not isinstance(payload, dict):
            return 400, {"error": "invalid json"}

        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not PRINCIPAL.fullmatch(username):
            return 400, {"error": "invalid username"}
        if not isinstance(password, str) or password == "":
            return 400, {"error": "password is required"}

        container_id = payload.get("container_id")
        log_type = payload.get("log_type")
        app_owner = payload.get("app_owner")
        pattern = payload.get("pattern")
        tail_lines = payload.get("tail_lines", self.tail_lines)
        if container_id is not None and (not isinstance(container_id, str) or not CONTAINER_ID.fullmatch(container_id)):
            return 400, {"error": "invalid container id"}
        if log_type is not None and log_type not in LOG_TYPES:
            return 400, {"error": "log_type must be stdout or stderr"}
        if app_owner is not None and (not isinstance(app_owner, str) or not PRINCIPAL.fullmatch(app_owner)):
            return 400, {"error": "invalid app owner"}
        if pattern is not None and not isinstance(pattern, str):
            return 400, {"error": "invalid pattern"}
        if not isinstance(tail_lines, int) or isinstance(tail_lines, bool) or not 1 <= tail_lines <= MAX_TAIL_LINES:
            return 400, {"error": "invalid tail_lines"}

        if not self._slots.acquire(timeout=self.acquire_timeout):
            return 429, {"error": "too many log requests"}
        try:
            return self._fetch(
                route,
                application_id,
                username,
                password,
                container_id,
                log_type,
                app_owner,
                pattern,
                tail_lines,
            )
        finally:
            self._slots.release()

    def _fetch(
        self,
        route: str,
        application_id: str,
        username: str,
        password: str,
        container_id: str | None,
        log_type: str | None,
        app_owner: str | None,
        pattern: str | None,
        tail_lines: int,
    ) -> tuple[int, dict[str, Any]]:
        logger.info(
            "action=%s application_id=%s container_id=%s user=%s",
            route,
            application_id,
            container_id or "-",
            username,
        )
        try:
            with self.authenticator.ticket(username, password) as ticket_env:
                if route == "list":
                    text = self.yarn.list_logs(application_id, ticket_env)
                else:
                    text = self.yarn.read_logs(
                        application_id,
                        ticket_env,
                        container_id=container_id,
                        log_type=log_type,
                        app_owner=app_owner,
                    )
        except AuthError:
            return 401, {"error": "kerberos authentication failed"}
        except YarnTimeout:
            return 504, {"error": "yarn logs timed out"}
        except YarnError as exc:
            return 502, {"error": "yarn logs failed", "detail": _redact(exc.detail, password)}
        return 200, {"text": _present(text, pattern if route == "content" else None, tail_lines, self.max_bytes, password)}


def _bearer_ok(header: str, token: str) -> bool:
    import secrets

    if not token or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header[len("Bearer ") :], token)


def _route(path: str) -> tuple[str | None, str | None]:
    prefix = "/applications/"
    if not path.startswith(prefix):
        return None, None
    rest = path[len(prefix) :]
    if rest.endswith("/logs"):
        application_id = rest[: -len("/logs")]
        kind = "list"
    elif rest.endswith("/logs/content"):
        application_id = rest[: -len("/logs/content")]
        kind = "content"
    else:
        return None, None
    if not APPLICATION_ID.fullmatch(application_id):
        return None, None
    return kind, application_id


def _present(text: str, pattern: str | None, tail_lines: int, max_bytes: int, secret: str) -> str:
    lines = text.splitlines()
    if pattern is not None:
        lines = [line for line in lines if pattern in line]
    lines = lines[-tail_lines:]
    rendered = _redact("\n".join(lines), secret)
    encoded = rendered.encode()
    if len(encoded) <= max_bytes:
        return rendered
    trimmed = encoded[-max_bytes:].decode(errors="ignore")
    return "[truncated]\n" + _redact(trimmed, secret)


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text
