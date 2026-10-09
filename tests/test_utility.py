import json
import os
import ssl
import subprocess
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from log_utility.auth import AuthError, KerberosAuthenticator
from log_utility.server import build_service
from log_utility.service import LogService
from log_utility.yarn import YarnRunner

FAKES = Path(__file__).parent / "fakes"
APP = "application_1700000000000_0001"
CONTAINER = "container_1700000000000_0001_01_000001"
PASSWORD = "s3cret-value"


class FakeAuth:
    def __init__(self, password: str = PASSWORD) -> None:
        self.password = password
        self.calls: list[str] = []

    @contextmanager
    def ticket(self, username: str, password: str):
        self.calls.append(username)
        if password != self.password:
            raise AuthError("kerberos authentication failed")
        yield {"KRB5CCNAME": "FILE:/tmp/spark-logs-test-cc"}


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> LogService:
    monkeypatch.setenv("YARN_FAKE_LOG", str(tmp_path / "yarn.log"))
    for key in ("YARN_FAKE_MODE", "YARN_FAKE_TEXT", "YARN_FAKE_STDERR", "EXPECTED_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return LogService(
        token="token",
        authenticator=FakeAuth(),
        yarn=YarnRunner(yarn_bin=str(FAKES / "yarn"), timeout=5, read_limit=100_000),
        max_bytes=128,
        tail_lines=200,
    )


def _post(service: LogService, path: str, payload: dict, token: str = "token") -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    return service.handle("POST", path, {"Authorization": f"Bearer {token}"}, body)


def test_list_runs_yarn_logs_as_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    status, payload = _post(
        service,
        f"/applications/{APP}/logs",
        {"username": "alice", "password": PASSWORD},
    )
    assert status == 200
    recorded = (tmp_path / "yarn.log").read_text()
    assert f"logs -applicationId {APP} -show_container_log_info" in recorded
    assert PASSWORD not in recorded
    assert "line-299" in payload["text"]
    assert "line-0\n" not in payload["text"]


def test_content_filters_literally_then_tails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch, YARN_FAKE_TEXT="keep me\nException: boom\n.*\nother")
    status, payload = _post(
        service,
        f"/applications/{APP}/logs/content",
        {
            "username": "alice",
            "password": PASSWORD,
            "container_id": CONTAINER,
            "log_type": "stderr",
            "pattern": "Exception",
            "tail_lines": 10,
        },
    )
    assert status == 200
    assert payload["text"] == "Exception: boom"
    recorded = (tmp_path / "yarn.log").read_text()
    assert f"-containerId {CONTAINER}" in recorded
    assert "-log_files stderr" in recorded
    assert PASSWORD not in recorded


def test_pattern_is_literal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch, YARN_FAKE_TEXT="alpha\n.*\nomega")
    status, payload = _post(
        service,
        f"/applications/{APP}/logs/content",
        {"username": "alice", "password": PASSWORD, "pattern": ".*"},
    )
    assert status == 200
    assert payload["text"] == ".*"


def test_rejects_shell_metacharacters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    status, _ = _post(
        service,
        "/applications/application_1_1;touch/logs",
        {"username": "alice", "password": PASSWORD},
    )
    assert status == 404
    status, payload = _post(
        service,
        f"/applications/{APP}/logs/content",
        {"username": "alice", "password": PASSWORD, "container_id": CONTAINER + ";touch /tmp/x"},
    )
    assert status == 400
    assert payload["error"] == "invalid container id"
    assert not (tmp_path / "yarn.log").exists()


def test_bad_token_and_bad_password_do_not_run_yarn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    status, _ = _post(service, f"/applications/{APP}/logs", {"username": "alice", "password": PASSWORD}, token="nope")
    assert status == 401
    status, payload = _post(service, f"/applications/{APP}/logs", {"username": "alice", "password": "wrong"})
    assert status == 401
    assert payload["error"] == "kerberos authentication failed"
    assert service.authenticator.calls == ["alice"]
    assert not (tmp_path / "yarn.log").exists()


def test_yarn_failure_redacts_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch, YARN_FAKE_MODE="fail", YARN_FAKE_STDERR=f"failed {PASSWORD}")
    status, payload = _post(service, f"/applications/{APP}/logs", {"username": "alice", "password": PASSWORD})
    assert status == 502
    assert PASSWORD not in payload["detail"]
    assert "[redacted]" in payload["detail"]


def test_truncates_large_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch, YARN_FAKE_MODE="big")
    status, payload = _post(service, f"/applications/{APP}/logs", {"username": "alice", "password": PASSWORD})
    assert status == 200
    assert payload["text"].startswith("[truncated]\n")
    assert len(payload["text"].encode()) < 200


def test_kerberos_password_is_not_an_argument(tmp_path: Path) -> None:
    argv_log = tmp_path / "kinit-argv"
    count = tmp_path / "kinit-count"
    destroyed = tmp_path / "kdestroy"
    os.environ["KINIT_ARGV_LOG"] = str(argv_log)
    os.environ["KINIT_COUNT"] = str(count)
    os.environ["EXPECTED_PASSWORD"] = PASSWORD
    os.environ["KDESTROY_LOG"] = str(destroyed)
    os.environ["YARN_FAKE_LOG"] = str(tmp_path / "yarn.log")
    service = LogService(
        token="token",
        authenticator=KerberosAuthenticator(
            kinit_bin=str(FAKES / "kinit"),
            kdestroy_bin=str(FAKES / "kdestroy"),
            timeout=5,
        ),
        yarn=YarnRunner(yarn_bin=str(FAKES / "yarn"), timeout=5),
    )
    status, _ = _post(service, f"/applications/{APP}/logs", {"username": "alice", "password": PASSWORD})
    assert status == 200
    assert PASSWORD not in argv_log.read_text()
    assert "alice" in argv_log.read_text()
    assert count.read_text() == "1"
    assert "kdestroy" in destroyed.read_text()
    assert "KRB5CCNAME=" in (tmp_path / "yarn.log").read_text()

    status, payload = _post(service, f"/applications/{APP}/logs", {"username": "alice", "password": "wrong"})
    assert status == 401
    assert payload["error"] == "kerberos authentication failed"
    assert count.read_text() == "2"


def test_rejected_password_is_not_retried(tmp_path: Path) -> None:
    count = tmp_path / "kinit-count"
    os.environ["KINIT_ARGV_LOG"] = str(tmp_path / "kinit-argv")
    os.environ["KINIT_COUNT"] = str(count)
    os.environ["EXPECTED_PASSWORD"] = PASSWORD
    os.environ["KDESTROY_LOG"] = str(tmp_path / "kdestroy")
    os.environ["YARN_FAKE_LOG"] = str(tmp_path / "yarn.log")
    service = LogService(
        token="token",
        authenticator=KerberosAuthenticator(kinit_bin=str(FAKES / "kinit"), kdestroy_bin=str(FAKES / "kdestroy")),
        yarn=YarnRunner(yarn_bin=str(FAKES / "yarn")),
    )
    _post(service, f"/applications/{APP}/logs", {"username": "alice", "password": "wrong"})
    assert count.read_text() == "1"
    assert not (tmp_path / "yarn.log").exists()


def test_build_service_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPARK_LOGS_API_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        build_service()


def test_main_requires_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARK_LOGS_API_TOKEN", "token")
    monkeypatch.delenv("SPARK_LOGS_TLS_CERT", raising=False)
    monkeypatch.delenv("SPARK_LOGS_TLS_KEY", raising=False)
    from log_utility.server import main

    with pytest.raises(SystemExit, match="TLS"):
        main()


def test_https_server_accepts_a_log_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("YARN_FAKE_LOG", str(tmp_path / "yarn.log"))
    monkeypatch.delenv("YARN_FAKE_MODE", raising=False)
    monkeypatch.delenv("YARN_FAKE_TEXT", raising=False)
    service = LogService(
        token="token",
        authenticator=FakeAuth(),
        yarn=YarnRunner(yarn_bin=str(FAKES / "yarn"), timeout=5),
        max_bytes=4000,
    )
    thread = threading.Thread(
        target=_serve_ephemeral,
        args=(service, str(cert), str(key), tmp_path),
        daemon=True,
    )
    thread.start()
    port_path = tmp_path / "port"
    for _ in range(50):
        if port_path.exists():
            break
        threading.Event().wait(0.05)
    port = int(port_path.read_text())
    context = ssl._create_unverified_context()
    request = urllib.request.Request(
        f"https://127.0.0.1:{port}/applications/{APP}/logs",
        data=json.dumps({"username": "alice", "password": PASSWORD}).encode(),
        method="POST",
        headers={"Authorization": "Bearer token", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        payload = json.loads(response.read().decode())
    assert "line-299" in payload["text"]


def _serve_ephemeral(service: LogService, cert: str, key: str, tmp_path: Path) -> None:
    from log_utility.server import _Server

    httpd = _Server(("127.0.0.1", 0), service)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    (tmp_path / "port").write_text(str(httpd.server_address[1]))
    httpd.serve_forever()
