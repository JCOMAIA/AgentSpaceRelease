"""The driver for a space that publishes but does not execute.

This is not a degraded mode. It is the shape you want when the space is open to
people you do not know: with no execution there is no sandbox to escape, so the
worst a stranger can do is publish a bad file. That is a moderation problem,
which a person can handle, rather than a remote code execution problem, which
they cannot.

The security property does not come from this file. It comes from the broker not
running and the Docker socket not being mounted — infrastructure, not a flag
someone can flip by editing `.env`. This driver exists so that an agent which
asks anyway gets taught what to do instead of receiving a stack trace.
"""

from __future__ import annotations

from ..teaching import NO_EXECUTION, AgentSpaceError
from .base import ExecResult, ExecSpec, ServiceHandle, ServiceSpec


def _refuse(what: str) -> AgentSpaceError:
    return AgentSpaceError(
        "execution_unavailable",
        f"{what} is not available on this space.",
        NO_EXECUTION,
        status_code=501,
        try_this={
            "transport": "rest",
            "method": "PUT",
            "path": "/api/v1/files?path=public/index.html",
            "body": {"content": "<h1>hello</h1>"},
            "note": "Anything under public/ is on the web the moment you write it.",
        },
    )


class NoExecutionDriver:
    """Refuses every execution request with an instruction, not an error code."""

    name = "none"

    async def run(self, spec: ExecSpec) -> ExecResult:
        raise _refuse("Running code")

    async def start_service(self, spec: ServiceSpec) -> ServiceHandle:
        raise _refuse("Publishing a running service")

    async def wake_service(self, container_id: str, port: int) -> ServiceHandle:
        raise _refuse("Waking a service")

    async def stop_service(self, container_id: str) -> None:
        return None

    async def remove_service(self, container_id: str) -> None:
        return None

    async def logs(self, container_id: str, tail: int = 200) -> str:
        raise _refuse("Reading service logs")

    async def is_available(self) -> bool:
        # True: the driver is working exactly as configured. Reporting False
        # would make preflight and /health call a deliberate choice a fault.
        return True
