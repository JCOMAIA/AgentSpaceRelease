"""Sandbox driver selection."""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import (
    ExecResult,
    ExecSpec,
    SandboxDriver,
    SandboxUnavailable,
    ServiceHandle,
    ServiceSpec,
)

__all__ = [
    "ExecResult",
    "ExecSpec",
    "SandboxDriver",
    "SandboxUnavailable",
    "ServiceHandle",
    "ServiceSpec",
    "get_driver",
]


@lru_cache
def get_driver() -> SandboxDriver:
    settings = get_settings()
    if settings.sandbox_driver == "none":
        # Publish-only. See none_driver for why this is a shape, not a downgrade.
        from .none_driver import NoExecutionDriver

        return NoExecutionDriver()

    if settings.sandbox_driver == "local_unsafe":
        from .local_driver import LocalUnsafeDriver

        return LocalUnsafeDriver()

    if settings.sandbox_driver == "remote":
        # The production shape: this process never touches the Docker socket.
        from .remote_driver import RemoteDriver

        return RemoteDriver(
            base_url=settings.sandbox_broker_url,
            token=settings.sandbox_broker_token,
        )

    from .docker_driver import DockerDriver

    return DockerDriver(
        image=settings.sandbox_image,
        network=settings.sandbox_network,
        allow_egress=settings.sandbox_allow_egress,
        runtime=settings.sandbox_runtime,
    )
