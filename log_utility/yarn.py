"""Run yarn logs and keep only the tail of its output."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import time


class YarnError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class YarnTimeout(Exception):
    pass


class YarnRunner:
    def __init__(self, yarn_bin: str = "yarn", timeout: float = 60, read_limit: int = 2_000_000) -> None:
        self.yarn_bin = yarn_bin
        self.timeout = timeout
        self.read_limit = read_limit

    def list_logs(self, application_id: str, env: dict[str, str]) -> str:
        command = [
            self.yarn_bin,
            "logs",
            "-applicationId",
            application_id,
            "-show_container_log_info",
        ]
        return self._run(command, env)

    def read_logs(
        self,
        application_id: str,
        env: dict[str, str],
        container_id: str | None = None,
        log_type: str | None = None,
        app_owner: str | None = None,
    ) -> str:
        command = [self.yarn_bin, "logs", "-applicationId", application_id]
        if app_owner:
            command.extend(["-appOwner", app_owner])
        if container_id:
            command.extend(["-containerId", container_id])
        if log_type:
            command.extend(["-log_files", log_type])
        return self._run(command, env)

    def _run(self, command: list[str], extra_env: dict[str, str]) -> str:
        process_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SPARK_LOGS_API_TOKEN", "WORKLOAD_PASSWORD"}
        }
        process_env.update(extra_env)
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
            start_new_session=True,
            bufsize=0,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        streams = [proc.stdout, proc.stderr]
        deadline = time.monotonic() + self.timeout
        try:
            while streams:
                if time.monotonic() > deadline:
                    _kill_group(proc)
                    raise YarnTimeout()
                ready, _, _ = select.select(streams, [], [], 0.2)
                for stream in ready:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        streams.remove(stream)
                        continue
                    buf = stdout if stream is proc.stdout else stderr
                    buf.extend(chunk)
                    if len(buf) > self.read_limit:
                        del buf[:-self.read_limit]
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                _kill_group(proc)
            proc.stdout.close()
            proc.stderr.close()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace")[-2000:]
            raise YarnError(detail or f"yarn logs exited {proc.returncode}")
        return stdout.decode(errors="replace")


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    proc.wait(timeout=5)
