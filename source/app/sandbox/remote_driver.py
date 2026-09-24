"""Sandbox driver that asks the broker instead of the Docker daemon.

This is what lets the control plane run without the Docker socket. It speaks the
same `SandboxDriver` protocol as `DockerDriver`, so nothing above it changes —
`operations.py` cannot tell which one it is holding.

Note what this driver does *not* send: no workspace path, no mount, no container
configuration. It sends a user id and the parameters that are legitimately the
caller's business. Everything dangerous is decided on the other side.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import (
    ExecResult,
    ExecSpec,
    SandboxUnavailable,
    ServiceHandle,
    ServiceSpec,
)

log = logging.getLogger(__name__)

# Long enough for a slow container start, short enough that a wedged broker
# surfaces as an error instead of hanging a request forever. Execution requests
# override it with the run's own timeout plus headroom.
DEFAULT_TIMEOUT = 60.0


class RemoteDriver:
    name = "remote"

    def __init__(self, base_url: str, token: str) -> None:
        if not base_url:
            raise ValueError("SANDBOX_BROKER_URL is required for the remote driver")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=DEFAULT_TIMEOUT,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http().request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SandboxUnavailable(f"cannot reach the sandbox broker: {exc}") from exc

        if response.status_code >= 500:
            raise SandboxUnavailable(f"broker error {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            # A 4xx is our own bad request, not an outage — surface it as such so
            # it is not retried as a transient failure.
            raise ValueError(f"broker rejected the request ({response.status_code}): "
                             f"{response.text[:200]}")
        return response.json()

    async def posture(self) -> dict[str, Any]:
        """Ask the broker what holding the socket is worth over there."""
        return await self._call("GET", "/posture")

    async def is_available(self) -> bool:
        try:
            body = await self._call("GET", "/health")
        except (SandboxUnavailable, ValueError):
            return False
        return bool(body.get("available"))

    async def run(self, spec: ExecSpec) -> ExecResult:
        body = await self._call(
            "POST",
            "/run",
            json={
                "user_id": spec.user_id,
                "language": spec.language,
                "code": spec.code,
                "timeout_s": spec.timeout_s,
                "memory_mb": spec.memory_mb,
                "cpus": spec.cpus,
                "allow_egress": spec.allow_egress,
            },
            # The broker will not answer before the run finishes, so wait longer
            # than the run itself is allowed to take.
            timeout=spec.timeout_s + 30,
        )
        return ExecResult(
            exit_code=int(body["exit_code"]),
            stdout=body.get("stdout", ""),
            stderr=body.get("stderr", ""),
            duration_ms=int(body.get("duration_ms", 0)),
            timed_out=bool(body.get("timed_out")),
        )

    async def start_service(self, spec: ServiceSpec) -> ServiceHandle:
        body = await self._call(
            "POST",
            "/services",
            json={
                "user_id": spec.user_id,
                "name": spec.name,
                "source_dir": spec.source_dir,
                "command": spec.command,
                "port": spec.port,
                "memory_mb": spec.memory_mb,
                "cpus": spec.cpus,
                "allow_egress": spec.allow_egress,
            },
            timeout=120.0,
        )
        return _handle(body)

    async def wake_service(self, container_id: str, port: int) -> ServiceHandle:
        body = await self._call(
            "POST", f"/services/{container_id}/wake", json={"port": port}, timeout=120.0
        )
        return _handle(body)

    async def stop_service(self, container_id: str) -> None:
        await self._call("POST", f"/services/{container_id}/stop")

    async def remove_service(self, container_id: str) -> None:
        await self._call("DELETE", f"/services/{container_id}")

    async def logs(self, container_id: str, tail: int = 200) -> str:
        body = await self._call(
            "GET", f"/services/{container_id}/logs", params={"tail": tail}
        )
        return body.get("logs", "")


def _handle(body: dict[str, Any]) -> ServiceHandle:
    return ServiceHandle(
        container_id=body["container_id"],
        internal_host=body["internal_host"],
        port=int(body["port"]),
    )
