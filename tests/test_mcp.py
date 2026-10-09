import asyncio
import json
from unittest.mock import patch

import pytest

from spark_logs.client import ClientError, get_application_logs, list_container_logs
from spark_logs.server import create_server


def test_client_refuses_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_API_URL", "http://logs.example")
    monkeypatch.setenv("SPARK_LOGS_API_TOKEN", "token")
    monkeypatch.setenv("WORKLOAD_USER", "alice")
    monkeypatch.setenv("WORKLOAD_PASSWORD", "s3cret-value")
    with pytest.raises(ClientError, match="https://"):
        list_container_logs("application_1700000000000_0001")


def test_client_sends_password_in_body_not_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_API_URL", "https://logs.example")
    monkeypatch.setenv("SPARK_LOGS_API_TOKEN", "token")
    monkeypatch.setenv("WORKLOAD_USER", "alice")
    monkeypatch.setenv("WORKLOAD_PASSWORD", "s3cret-value")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"text": "Exception: boom"}'

    def urlopen(request, context=None, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode())
        return Response()

    with patch("spark_logs.client.urllib.request.urlopen", urlopen):
        text = get_application_logs(
            "application_1700000000000_0001",
            container_id="container_1700000000000_0001_01_000001",
            log_type="stderr",
            pattern="Exception",
        )
    assert text == "Exception: boom"
    assert "s3cret-value" not in captured["url"]
    assert captured["auth"] == "Bearer token"
    assert captured["body"]["password"] == "s3cret-value"
    assert captured["body"]["log_type"] == "stderr"


def test_tool_schema_has_no_password() -> None:
    server = create_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == {"list_container_logs", "get_application_logs"}
    encoded = json.dumps(tools["get_application_logs"].input_schema)
    assert "password" not in encoded
    assert "application_id" in encoded


def test_tool_returns_utility_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_API_URL", "https://logs.example")
    monkeypatch.setenv("SPARK_LOGS_API_TOKEN", "token")
    monkeypatch.setenv("WORKLOAD_USER", "alice")
    monkeypatch.setenv("WORKLOAD_PASSWORD", "s3cret-value")
    server = create_server()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"text": "container listing"}'

    with patch("spark_logs.client.urllib.request.urlopen", lambda *args, **kwargs: Response()):
        result = asyncio.run(
            server.call_tool("list_container_logs", {"application_id": "application_1700000000000_0001"})
        )
    text = _tool_text(result)
    assert text == "container listing"


def _tool_text(result: object) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, tuple):
        content = content[0]
    first = content[0]
    return getattr(first, "text", str(first))
