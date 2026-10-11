"""Obtain a short-lived Kerberos ticket without placing the password on argv."""

from __future__ import annotations

import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager


class AuthError(Exception):
    """Kerberos rejected the workload password, or kinit did not finish."""


class KerberosAuthenticator:
    def __init__(self, kinit_bin: str = "kinit", kdestroy_bin: str = "kdestroy", timeout: float = 30) -> None:
        self.kinit_bin = kinit_bin
        self.kdestroy_bin = kdestroy_bin
        self.timeout = timeout

    @contextmanager
    def ticket(self, username: str, password: str) -> Iterator[dict[str, str]]:
        directory = tempfile.mkdtemp(prefix="spark-logs-cc-")
        os.chmod(directory, 0o700)
        ccache = os.path.join(directory, "ccache")
        try:
            self._kinit(username, password, ccache)
            yield {"KRB5CCNAME": f"FILE:{ccache}"}
        finally:
            self._kdestroy(ccache)
            shutil.rmtree(directory, ignore_errors=True)

    def _kinit(self, username: str, password: str, ccache: str) -> None:
        # kinit reads the password from its controlling terminal. pty.fork()
        # makes the slave that terminal, so the password is not typed into
        # the operator's SSH session.
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execvp(self.kinit_bin, [self.kinit_bin, "-c", ccache, username])
            finally:
                os._exit(127)
        try:
            status = self._read_until(master, pid, b"Password")
            if status is None:
                try:
                    os.write(master, password.encode() + b"\n")
                except OSError as exc:
                    self._stop(pid)
                    raise AuthError("kerberos authentication failed") from exc
                status = self._read_until(master, pid, None)
            if status is None or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
                raise AuthError("kerberos authentication failed")
        finally:
            os.close(master)

    def _read_until(self, master: int, pid: int, token: bytes | None) -> int | None:
        deadline = time.monotonic() + self.timeout
        seen = b""
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return status
            if time.monotonic() > deadline:
                self._stop(pid)
                raise AuthError("kerberos authentication failed")
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                chunk = b""
            seen += chunk
            if token is not None and token in seen:
                return None

    def _stop(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        os.waitpid(pid, 0)

    def _kdestroy(self, ccache: str) -> None:
        subprocess.run(
            [self.kdestroy_bin, "-c", ccache],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
