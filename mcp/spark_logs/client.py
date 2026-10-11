"""Call the yarn logs utility. Credentials come from the process environment."""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any


class ClientError(Exception):
    pass


def list_container_logs(application_id: str) -> str:
    return _post(f"/applications/{application_id}/logs", {})


def get_application_logs(
    application_id: str,
    container_id: str | None = None,
    log_type: str | None = None,
    pattern: str | None = None,
    tail_lines: int | None = None,
    app_owner: str | None = None,
) -> str:
    payload: dict[str, Any] = {}
    if container_id:
        payload["container_id"] = container_id
    if log_type:
        payload["log_type"] = log_type
    if pattern:
        payload["pattern"] = pattern
    if tail_lines is not None:
        payload["tail_lines"] = tail_lines
    if app_owner:
        payload["app_owner"] = app_owner
    return _post(f"/applications/{application_id}/logs/content", payload)


def certificate_pem(raw: str) -> str:
    """Turn a pasted certificate into a PEM block with 64-character lines."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "BEGIN CERTIFICATE" not in text and "PRIVATE KEY" in text:
        raise ClientError("SPARK_LOGS_CA_PEM must be server.pem, not the private key")
    match = re.search(
        r"-----BEGIN CERTIFICATE-----(.*)-----END CERTIFICATE-----",
        text,
        re.DOTALL,
    )
    if not match:
        raise ClientError("SPARK_LOGS_CA_PEM is not a valid certificate")
    body = re.sub(r"[^A-Za-z0-9+/=]", "", match.group(1))
    if not body:
        raise ClientError("SPARK_LOGS_CA_PEM is not a valid certificate")
    lines = [body[index : index + 64] for index in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


def ssl_context() -> ssl.SSLContext:
    """Trust the default CAs, plus a pasted PEM or a CA file when one is set."""
    context = ssl.create_default_context()
    pem = os.environ.get("SPARK_LOGS_CA_PEM", "").strip()
    if pem:
        try:
            context.load_verify_locations(cadata=certificate_pem(pem))
        except ssl.SSLError as exc:
            raise ClientError("SPARK_LOGS_CA_PEM is not a valid certificate") from exc
        return context
    ca_cert = os.environ.get("SPARK_LOGS_CA_CERT", "").strip()
    if ca_cert:
        context.load_verify_locations(cafile=ca_cert)
    return context


def _post(path: str, payload: dict[str, Any]) -> str:
    base = os.environ.get("SPARK_LOGS_API_URL", "").rstrip("/")
    if not base.startswith("https://"):
        raise ClientError("SPARK_LOGS_API_URL must start with https://")
    username = os.environ.get("WORKLOAD_USER", "")
    password = os.environ.get("WORKLOAD_PASSWORD", "")
    token = os.environ.get("SPARK_LOGS_API_TOKEN", "")
    if not username or not password or not token:
        raise ClientError("WORKLOAD_USER, WORKLOAD_PASSWORD, and SPARK_LOGS_API_TOKEN are required")
    body = json.dumps({"username": username, "password": password, **payload}).encode()
    request = urllib.request.Request(
        base + path,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    context = ssl_context()
    try:
        with urllib.request.urlopen(request, context=context, timeout=120) as response:
            parsed = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        message = "log utility request failed"
        try:
            parsed = json.loads(exc.read().decode())
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                message = parsed["error"]
                detail = parsed.get("detail")
                if isinstance(detail, str) and detail:
                    message = f"{message}: {detail}"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise ClientError(message) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise ClientError("log utility request failed") from None
    text = parsed.get("text") if isinstance(parsed, dict) else None
    if not isinstance(text, str):
        raise ClientError("log utility request failed")
    return text
