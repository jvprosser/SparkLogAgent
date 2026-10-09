"""Obtain a short-lived Kerberos ticket without placing the password on argv."""

from __future__ import annotations

import os
import pty
import select
import shutil
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
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                [self.kinit_bin, "-c", ccache, username],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
            )
            os.close(slave)
            slave = -1
            try:
                os.write(master, password.encode() + b"\n")
            except OSError as exc:
                raise AuthError("kerberos authentication failed") from exc
            deadline = time.monotonic() + self.timeout
            while proc.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if master in ready:
                    try:
                        os.read(master, 4096)
                    except OSError:
                        break
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
                raise AuthError("kerberos authentication failed")
            if proc.returncode != 0:
                raise AuthError("kerberos authentication failed")
        finally:
            if slave >= 0:
                os.close(slave)
            os.close(master)

    def _kdestroy(self, ccache: str) -> None:
        subprocess.run(
            [self.kdestroy_bin, "-c", ccache],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
