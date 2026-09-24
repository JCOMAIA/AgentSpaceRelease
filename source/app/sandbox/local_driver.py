"""Subprocess driver — development and CI only.

THIS PROVIDES NO ISOLATION. Code runs as the server user with full access to the
host. It exists so the test suite and a laptop without Docker can exercise the
rest of the system. `get_driver()` refuses to select it unless the operator sets
SANDBOX_DRIVER=local_unsafe by hand.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio

from .base import LANGUAGES, ExecResult, ExecSpec, ServiceHandle, ServiceSpec

log = logging.getLogger(__name__)


class LocalUnsafeDriver:
    name = "local_unsafe"

    def __init__(self) -> None:
        log.warning(
            "SANDBOX DRIVER 'local_unsafe' IS ACTIVE — user code runs unisolated on the host. "
            "Never enable this on a server that accepts registrations."
        )

    async def is_available(self) -> bool:
        return True

    async def run(self, spec: ExecSpec) -> ExecResult:
        return await anyio.to_thread.run_sync(self._run_sync, spec)

    def _run_sync(self, spec: ExecSpec) -> ExecResult:
        lang = LANGUAGES[spec.language]
        argv = list(lang["argv"])
        if spec.language == "python":
            argv = [sys.executable, "-u"]

        with tempfile.TemporaryDirectory(prefix="agentspace-local-") as tmp:
            script = Path(tmp) / f"main{lang['ext']}"
            script.write_text(spec.code, encoding="utf-8")
            started = time.monotonic()
            timed_out = False
            try:
                proc = subprocess.run(
                    [*argv, str(script)],
                    cwd=str(spec.workspace_path),
                    capture_output=True,
                    timeout=spec.timeout_s,
                    text=True,
                    errors="replace",
                )
                exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = -1
                stdout = exc.stdout or ""
                stderr = (exc.stderr or "") + f"\n[agentspace] killed after {spec.timeout_s}s"
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", "replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", "replace")
            except FileNotFoundError as exc:
                exit_code, stdout = 127, ""
                stderr = f"[agentspace] runtime not installed on this host: {exc}"

            return ExecResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=timed_out,
            )

    async def start_service(self, spec: ServiceSpec) -> ServiceHandle:
        raise NotImplementedError(
            "The local driver cannot host services. Use the docker driver for deployments."
        )

    async def wake_service(self, container_id: str, port: int) -> ServiceHandle:
        raise NotImplementedError(
            "The local driver cannot host services. Use the docker driver for deployments."
        )

    async def stop_service(self, container_id: str) -> None:
        return None

    async def remove_service(self, container_id: str) -> None:
        return None

    async def logs(self, container_id: str, tail: int = 200) -> str:
        return "[agentspace] the local driver does not keep service logs."
