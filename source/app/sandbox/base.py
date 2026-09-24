"""Sandbox driver interface.

Kept narrow on purpose: swapping Docker for Firecracker microVMs or gVisor means
implementing these six methods, nothing else in the codebase moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

LANGUAGES = {
    "python": {"ext": ".py", "argv": ["python", "-u"]},
    "node": {"ext": ".js", "argv": ["node"]},
    "bash": {"ext": ".sh", "argv": ["bash"]},
}


@dataclass
class ExecSpec:
    user_id: str
    workspace_path: Path
    language: str
    code: str
    timeout_s: int
    memory_mb: int
    cpus: float
    allow_egress: bool = False
    workdir: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


@dataclass
class ServiceSpec:
    user_id: str
    name: str
    workspace_path: Path
    source_dir: str
    command: str
    port: int
    memory_mb: int
    cpus: float
    allow_egress: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ServiceHandle:
    container_id: str
    internal_host: str
    port: int


@runtime_checkable
class SandboxDriver(Protocol):
    name: str

    async def run(self, spec: ExecSpec) -> ExecResult: ...

    async def start_service(self, spec: ServiceSpec) -> ServiceHandle: ...

    async def wake_service(self, container_id: str, port: int) -> ServiceHandle: ...

    async def stop_service(self, container_id: str) -> None: ...

    async def remove_service(self, container_id: str) -> None: ...

    async def logs(self, container_id: str, tail: int = 200) -> str: ...

    async def is_available(self) -> bool: ...


class SandboxUnavailable(RuntimeError):
    """Raised when the driver cannot reach its backend (e.g. Docker is down)."""
