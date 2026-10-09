"""TLS HTTP server in front of LogService."""

from __future__ import annotations

import json
import logging
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from log_utility.auth import KerberosAuthenticator
from log_utility.service import LogService
from log_utility.yarn import YarnRunner

logger = logging.getLogger("sparklogagent.utility")


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 16384:
            self._send(413, {"error": "request too large"})
            return
        body = self.rfile.read(length) if length else b""
        status, payload = self.server.service.handle("POST", self.path, self.headers, body)
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._send(405, {"error": "method not allowed"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], service: LogService) -> None:
        super().__init__(address, _Handler)
        self.service = service


def build_service() -> LogService:
    token = os.environ.get("SPARK_LOGS_API_TOKEN", "")
    if not token:
        raise SystemExit("SPARK_LOGS_API_TOKEN is required")
    return LogService(
        token=token,
        authenticator=KerberosAuthenticator(
            kinit_bin=os.environ.get("SPARK_LOGS_KINIT", "kinit"),
            kdestroy_bin=os.environ.get("SPARK_LOGS_KDESTROY", "kdestroy"),
        ),
        yarn=YarnRunner(
            yarn_bin=os.environ.get("SPARK_LOGS_YARN", "yarn"),
            timeout=float(os.environ.get("SPARK_LOGS_TIMEOUT", "60")),
            read_limit=int(os.environ.get("SPARK_LOGS_READ_LIMIT", "2000000")),
        ),
        max_bytes=int(os.environ.get("SPARK_LOGS_MAX_BYTES", "65536")),
        tail_lines=int(os.environ.get("SPARK_LOGS_TAIL_LINES", "200")),
        concurrency=int(os.environ.get("SPARK_LOGS_MAX_CONCURRENCY", "4")),
    )


def serve(service: LogService, host: str, port: int, cert: str, key: str) -> None:
    httpd = _Server((host, port), service)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    logger.info("listening on %s:%s", host, port)
    httpd.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    cert = os.environ.get("SPARK_LOGS_TLS_CERT", "")
    key = os.environ.get("SPARK_LOGS_TLS_KEY", "")
    if not cert or not key:
        raise SystemExit("SPARK_LOGS_TLS_CERT and SPARK_LOGS_TLS_KEY are required")
    host = os.environ.get("SPARK_LOGS_BIND", "127.0.0.1")
    port = int(os.environ.get("SPARK_LOGS_PORT", "8443"))
    serve(build_service(), host, port, cert, key)
