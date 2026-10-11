import asyncio
import json
import subprocess
import urllib.error
from unittest.mock import patch

import pytest

from spark_logs.client import ClientError, gateway_status, get_application_logs, list_container_logs, ssl_context
from spark_logs.server import create_server


def test_client_refuses_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_API_URL", "http://logs.example")
    monkeypatch.setenv("SPARK_LOGS_API_TOKEN", "token")
    monkeypatch.setenv("WORKLOAD_USER", "alice")
    monkeypatch.setenv("WORKLOAD_PASSWORD", "s3cret-value")
    with pytest.raises(ClientError, match="https://"):
        list_container_logs("application_1700000000000_0001")


def test_ca_pem_is_trusted_when_newlines_are_escaped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cert = tmp_path / "server.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(tmp_path / "server.key"),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=spark-logs-test",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("SPARK_LOGS_CA_PEM", cert.read_text().replace("\n", "\\n"))
    monkeypatch.delenv("SPARK_LOGS_CA_CERT", raising=False)
    subjects = [str(item.get("subject", "")) for item in ssl_context().get_ca_certs()]
    assert any("spark-logs-test" in subject for subject in subjects)


def test_ca_pem_is_trusted_when_it_is_one_line(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cert = tmp_path / "server.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(tmp_path / "server.key"),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=spark-logs-oneline",
        ],
        check=True,
        capture_output=True,
    )
    one_line = "".join(cert.read_text().splitlines())
    monkeypatch.setenv("SPARK_LOGS_CA_PEM", one_line)
    monkeypatch.delenv("SPARK_LOGS_CA_CERT", raising=False)
    subjects = [str(item.get("subject", "")) for item in ssl_context().get_ca_certs()]
    assert any("spark-logs-oneline" in subject for subject in subjects)


def test_private_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_CA_PEM", "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----")
    with pytest.raises(ClientError, match="private key"):
        ssl_context()


def test_invalid_ca_pem_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_CA_PEM", "-----BEGIN CERTIFICATE-----\\nnot-a-cert\\n-----END CERTIFICATE-----")
    with pytest.raises(ClientError, match="SPARK_LOGS_CA_PEM"):
        ssl_context()


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
    assert set(tools) == {"gateway_status", "list_container_logs", "get_application_logs"}
    assert tools["gateway_status"].input_schema.get("required", []) == []
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


def test_gateway_status_reports_both_git_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_API_URL", "https://logs.example:8443")
    monkeypatch.setenv("SPARK_LOGS_API_TOKEN", "token")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status": "ok", "git_version": "abc123"}'

    def urlopen(request, context=None, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.get_header("Authorization")
        captured["data"] = request.data
        return Response()

    with patch("spark_logs.client.urllib.request.urlopen", urlopen):
        text = gateway_status()
    assert captured["url"] == "https://logs.example:8443/status"
    assert captured["method"] == "GET"
    assert captured["auth"] == "Bearer token"
    assert captured["data"] is None
    assert "git_version: " in text
    assert "gateway: connected" in text
    assert "gateway_git_version: abc123" in text
    assert "token" not in text.split("git_version: ", 1)[-1].split("\n", 1)[0]


def test_gateway_status_reports_a_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_API_URL", "https://logs.example:8443")
    monkeypatch.setenv("SPARK_LOGS_API_TOKEN", "token")

    def urlopen(request, context=None, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("spark_logs.client.urllib.request.urlopen", urlopen):
        text = gateway_status()
    assert "gateway: unreachable" in text
    assert "connection refused" in text


def _tool_text(result: object) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, tuple):
        content = content[0]
    first = content[0]
    return getattr(first, "text", str(first))
