"""The only process that talks to the Docker daemon.

Holding the Docker socket is equivalent to holding root on the host: anyone who
can reach it can start a container that bind-mounts `/` and chroots into it. The
control plane serves untrusted input all day, so giving it that power means one
bug in a request handler costs the whole machine — the database, every user's
files, the Stripe key, the TLS keys.

This service exists so the control plane does not have it. It exposes exactly
the seven operations of `SandboxDriver` and nothing else, and it decides every
dangerous parameter itself:

  * **Paths are derived, never accepted.** A workspace path is computed from the
    user id. There is no request field that can point a mount at `/etc`.
  * **Resources are clamped.** A caller asking for 100 GB gets the configured
    ceiling.
  * **Ids are validated.** `user_id` must be a hex id, names and directories are
    checked for traversal before they reach a mount specification.

A compromised control plane can therefore still run code as any user — which is
bad — but cannot read the database, cannot reach the host filesystem, and cannot
start a privileged container.

Run it as its own service, on an internal network, never published:

    uvicorn app.broker:app --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import hmac
import logging
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import get_settings
from .sandbox.base import ExecSpec, SandboxUnavailable, ServiceSpec
from .sandbox.docker_driver import DockerDriver

log = logging.getLogger("agentspace.broker")

USER_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    if not settings.sandbox_broker_token:
        raise RuntimeError(
            "refusing to start: SANDBOX_BROKER_TOKEN is empty, which would leave "
            "privileged sandbox operations open to anything that can reach this port"
        )
    # The runtime check moved here with the daemon: this is now the only process
    # that can see which runtimes Docker has registered.
    if settings.sandbox_runtime:
        try:
            await anyio.to_thread.run_sync(driver().verify_runtime)
        except SandboxUnavailable as exc:
            if settings.sandbox_require_runtime:
                raise RuntimeError(f"refusing to start: {exc}") from exc
            log.warning("%s — continuing on the default runtime", exc)
        else:
            log.info("sandbox runtime %r is available", settings.sandbox_runtime)

    await check_daemon_privilege(settings)
    log.info("sandbox broker ready")
    yield


async def check_daemon_privilege(settings) -> None:
    """Say out loud what holding this socket is currently worth.

    Against a rootful daemon it is root on the host, so the boundary this
    service creates is narrower than it looks. That is a deployment fact, not a
    code one, and the only useful thing code can do is refuse to let it be
    invisible.
    """
    try:
        rootless = await anyio.to_thread.run_sync(driver().daemon_is_rootless)
    except SandboxUnavailable as exc:
        log.warning("could not determine daemon privilege: %s", exc)
        return

    if rootless:
        log.info("docker daemon is rootless — socket access is not host root")
        return

    message = (
        "DOCKER DAEMON IS ROOTFUL. Anything that compromises this broker gets root on "
        "the host. Run the daemon rootless and point DOCKER_HOST at its socket — see "
        "docs/DEPLOY_KIMSUFI.md."
    )
    if settings.sandbox_require_rootless:
        raise RuntimeError(f"refusing to start: {message}")
    log.warning(message)


app = FastAPI(
    title="AgentSpace sandbox broker",
    description="Privileged sandbox operations. Internal only — never publish this.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

_driver: DockerDriver | None = None


def driver() -> DockerDriver:
    global _driver
    if _driver is None:
        settings = get_settings()
        _driver = DockerDriver(
            image=settings.sandbox_image,
            network=settings.sandbox_network,
            allow_egress=settings.sandbox_allow_egress,
            runtime=settings.sandbox_runtime,
        )
    return _driver


async def authorise(authorization: Annotated[str | None, Header()] = None) -> None:
    """Shared-secret auth. The network is the first line; this is the second."""
    settings = get_settings()
    if not settings.sandbox_broker_token:
        raise HTTPException(503, "broker token is not configured")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not hmac.compare_digest(presented, settings.sandbox_broker_token):
        raise HTTPException(401, "invalid broker token")


Auth = Depends(authorise)


# --------------------------------------------------------------------------
# Validation — the whole point of this service
# --------------------------------------------------------------------------
def workspace_for(user_id: str):
    """Derive the workspace path. Never accept one from the caller."""
    if not USER_ID_RE.match(user_id):
        raise HTTPException(400, "user_id must be a hex identifier")
    root = get_settings().data_root_abs / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_relative(value: str, field: str) -> str:
    """Reject anything that could climb out of the workspace once mounted."""
    cleaned = (value or ".").strip().replace("\\", "/")
    if cleaned in ("", "."):
        return "."
    if cleaned.startswith("/") or ".." in cleaned.split("/"):
        raise HTTPException(400, f"{field} must be a relative path inside the workspace")
    return cleaned


def clamp(memory_mb: int, cpus: float, timeout_s: int) -> tuple[int, float, int]:
    settings = get_settings()
    return (
        max(64, min(int(memory_mb), settings.broker_max_memory_mb)),
        max(0.1, min(float(cpus), settings.broker_max_cpus)),
        max(1, min(int(timeout_s), settings.broker_max_timeout_s)),
    )


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class RunRequest(BaseModel):
    user_id: str
    language: str
    code: str
    timeout_s: int = 60
    memory_mb: int = 512
    cpus: float = 0.5
    allow_egress: bool = False


class ServiceRequest(BaseModel):
    user_id: str
    name: str
    source_dir: str = "."
    command: str
    port: int = Field(ge=1024, le=65535)
    memory_mb: int = 512
    cpus: float = 0.5
    allow_egress: bool = False


class WakeRequest(BaseModel):
    port: int = Field(ge=1, le=65535)


def _unavailable(exc: Exception) -> HTTPException:
    log.warning("sandbox backend unavailable: %s", exc)
    return HTTPException(503, f"sandbox backend unavailable: {exc}")


# --------------------------------------------------------------------------
# Endpoints — one per SandboxDriver method, and nothing else
# --------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    """Unauthenticated on purpose: compose health checks must not need the secret.
    Deliberately says nothing about the host."""
    return {"status": "ok", "available": await driver().is_available()}


@app.get("/posture", dependencies=[Auth])
async def posture() -> dict[str, Any]:
    """What holding this socket is currently worth.

    The control plane cannot see the daemon any more — that is the whole point —
    so without this its preflight check would quietly stop reporting whether the
    daemon is rootless, and read as safe when it is not.
    """
    backend = driver()
    try:
        rootless = await anyio.to_thread.run_sync(backend.daemon_is_rootless)
        runtimes = sorted(await anyio.to_thread.run_sync(backend.available_runtimes))
    except SandboxUnavailable as exc:
        raise HTTPException(503, f"docker daemon unreachable: {exc}") from exc

    # Read from the driver, not from settings: what matters is the runtime the
    # thing that starts containers will actually use.
    wanted = backend.runtime
    return {
        "rootless": rootless,
        "runtime": wanted,
        "runtime_available": wanted in runtimes if wanted else False,
        "runtimes": runtimes,
        "allow_egress": backend.allow_egress,
    }


@app.post("/run", dependencies=[Auth])
async def run(body: RunRequest) -> dict[str, Any]:
    memory_mb, cpus, timeout_s = clamp(body.memory_mb, body.cpus, body.timeout_s)
    spec = ExecSpec(
        user_id=body.user_id,
        workspace_path=workspace_for(body.user_id),
        language=body.language,
        code=body.code,
        timeout_s=timeout_s,
        memory_mb=memory_mb,
        cpus=cpus,
        allow_egress=body.allow_egress,
    )
    try:
        result = await driver().run(spec)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except SandboxUnavailable as exc:
        raise _unavailable(exc) from exc
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }


@app.post("/services", dependencies=[Auth])
async def start_service(body: ServiceRequest) -> dict[str, Any]:
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "name must be a lowercase slug")
    memory_mb, cpus, _ = clamp(body.memory_mb, body.cpus, 60)
    spec = ServiceSpec(
        user_id=body.user_id,
        name=body.name,
        workspace_path=workspace_for(body.user_id),
        source_dir=safe_relative(body.source_dir, "source_dir"),
        command=body.command,
        port=body.port,
        memory_mb=memory_mb,
        cpus=cpus,
        allow_egress=body.allow_egress,
    )
    try:
        handle = await driver().start_service(spec)
    except SandboxUnavailable as exc:
        raise _unavailable(exc) from exc
    return {
        "container_id": handle.container_id,
        "internal_host": handle.internal_host,
        "port": handle.port,
    }


@app.post("/services/{container_id}/wake", dependencies=[Auth])
async def wake_service(container_id: str, body: WakeRequest) -> dict[str, Any]:
    try:
        handle = await driver().wake_service(container_id, body.port)
    except SandboxUnavailable as exc:
        raise _unavailable(exc) from exc
    return {
        "container_id": handle.container_id,
        "internal_host": handle.internal_host,
        "port": handle.port,
    }


@app.post("/services/{container_id}/stop", dependencies=[Auth])
async def stop_service(container_id: str) -> dict[str, Any]:
    await driver().stop_service(container_id)
    return {"stopped": True}


@app.delete("/services/{container_id}", dependencies=[Auth])
async def remove_service(container_id: str) -> dict[str, Any]:
    await driver().remove_service(container_id)
    return {"removed": True}


@app.get("/services/{container_id}/logs", dependencies=[Auth])
async def logs(container_id: str, tail: int = 200) -> dict[str, Any]:
    return {"logs": await driver().logs(container_id, tail=max(1, min(tail, 5000)))}
