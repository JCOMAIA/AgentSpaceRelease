"""Docker-backed sandbox.

Hardening applied to every container we start:
  * no capabilities, `no-new-privileges`, non-root uid
  * read-only rootfs; only the workspace bind and a small tmpfs are writable
  * memory, CPU and pid ceilings from the user's plan
  * no network at all unless the plan explicitly allows egress
  * the Docker socket is never exposed

That is meaningful isolation but it is still a shared kernel. Treat it as
"untrusted code, trusted kernel" and plan for microVMs before selling a tier
that promises stronger separation.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

import anyio

from .base import (
    LANGUAGES,
    ExecResult,
    ExecSpec,
    SandboxUnavailable,
    ServiceHandle,
    ServiceSpec,
)

log = logging.getLogger(__name__)

SANDBOX_UID = 10001
PIDS_LIMIT = 256
TMPFS = {"/tmp": "rw,noexec,nosuid,size=64m"}
OUTPUT_LIMIT = 256 * 1024  # bytes of stdout/stderr we hand back


def _control_plane_is_containerised() -> bool:
    """Whether *we* run inside Docker, which decides how we reach a service.

    In production the app container shares `agentspace_sandbox` with user
    services, so the container name resolves and nothing needs publishing.
    Running on a developer's host — Windows or macOS especially — that name is
    unreachable, so the service port has to be published on the loopback
    interface instead. Getting this wrong looks like every deployed service
    timing out, so it is decided here rather than left to configuration.
    """
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


class DockerDriver:
    name = "docker"

    def __init__(
        self,
        image: str,
        network: str,
        allow_egress: bool,
        runtime: str = "",
    ) -> None:
        self.image = image
        self.network = network
        self.allow_egress = allow_egress
        self.runtime = runtime.strip()
        self._client = None

    # -- plumbing ---------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            try:
                import docker  # imported lazily so the app boots without Docker
            except ImportError as exc:  # pragma: no cover - dependency present in prod
                raise SandboxUnavailable("the `docker` package is not installed") from exc
            try:
                self._client = docker.from_env()
                self._client.ping()
            except Exception as exc:
                self._client = None
                raise SandboxUnavailable(f"cannot reach the Docker daemon: {exc}") from exc
        return self._client

    async def is_available(self) -> bool:
        try:
            await anyio.to_thread.run_sync(self._get_client)
        except SandboxUnavailable:
            return False
        return True

    def _network_mode(self, allow_egress: bool) -> str:
        return self.network if (allow_egress and self.allow_egress) else "none"

    def available_runtimes(self) -> set[str]:
        info = self._get_client().info()
        return set((info.get("Runtimes") or {}).keys())

    def daemon_is_rootless(self) -> bool:
        """Whether the daemon runs unprivileged.

        This decides what socket access is worth. Against a rootful daemon it is
        root on the host; against a rootless one it is the privileges of one
        unprivileged account, which is the difference between losing the machine
        and losing a service.

        Docker reports it in `SecurityOptions` as a `name=rootless` entry.
        """
        info = self._get_client().info()
        for option in info.get("SecurityOptions") or []:
            for part in str(option).split(","):
                if part.strip() == "name=rootless":
                    return True
        return False

    def verify_runtime(self) -> None:
        """Fail loudly when the configured runtime is not installed.

        Docker's own behaviour on an unknown runtime is to reject the container,
        so every execution would fail with a confusing error. Checking once at
        boot turns that into one clear message.
        """
        if not self.runtime:
            return
        available = self.available_runtimes()
        if self.runtime not in available:
            raise SandboxUnavailable(
                f"sandbox runtime {self.runtime!r} is not registered with Docker "
                f"(available: {', '.join(sorted(available)) or 'none'}). "
                "Install it and add it to /etc/docker/daemon.json, or clear SANDBOX_RUNTIME "
                "to fall back to runc — which shares the host kernel."
            )

    def _common_kwargs(self, memory_mb: int, cpus: float, allow_egress: bool) -> dict:
        extra = {"runtime": self.runtime} if self.runtime else {}
        return {
            **extra,
            "mem_limit": f"{memory_mb}m",
            # Must equal mem_limit. Docker's default when this is unset is twice
            # the memory limit, the surplus served from swap — so a "512 MB" plan
            # silently becomes a 1 GB plan, and on spinning disks the box thrashes
            # instead of OOM-killing the one container that misbehaved.
            "memswap_limit": f"{memory_mb}m",
            # Docker wants CPU quota in billionths of a core.
            "nano_cpus": int(cpus * 1_000_000_000),
            "pids_limit": PIDS_LIMIT,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "user": f"{SANDBOX_UID}:{SANDBOX_UID}",
            "read_only": True,
            "tmpfs": dict(TMPFS),
            "network_mode": self._network_mode(allow_egress),
            "privileged": False,
        }

    # -- one-shot execution -----------------------------------------------
    async def run(self, spec: ExecSpec) -> ExecResult:
        if spec.language not in LANGUAGES:
            raise ValueError(f"unsupported language: {spec.language}")
        return await anyio.to_thread.run_sync(self._run_sync, spec)

    def _run_sync(self, spec: ExecSpec) -> ExecResult:
        client = self._get_client()
        lang = LANGUAGES[spec.language]

        # The program itself lives outside the workspace, mounted read-only, so
        # a run never litters the user's files.
        staging = Path(tempfile.mkdtemp(prefix="agentspace-exec-"))
        try:
            script = staging / f"main{lang['ext']}"
            script.write_text(spec.code, encoding="utf-8")
            script.chmod(0o444)

            kwargs = self._common_kwargs(spec.memory_mb, spec.cpus, spec.allow_egress)
            container = client.containers.create(
                self.image,
                command=[*lang["argv"], f"/opt/agentspace/main{lang['ext']}"],
                working_dir=spec.workdir,
                environment={"HOME": "/tmp", "AGENTSPACE": "1", **spec.env},
                volumes={
                    str(spec.workspace_path): {"bind": "/workspace", "mode": "rw"},
                    str(staging): {"bind": "/opt/agentspace", "mode": "ro"},
                },
                labels={"agentspace.user": spec.user_id, "agentspace.kind": "exec"},
                **kwargs,
            )
            started = time.monotonic()
            timed_out = False
            exit_code = -1
            try:
                container.start()
                result = container.wait(timeout=spec.timeout_s)
                exit_code = int(result.get("StatusCode", -1))
            except Exception as exc:
                # `wait` raising is how the SDK surfaces the timeout.
                timed_out = True
                log.info("exec timeout for user=%s: %s", spec.user_id, exc)
                try:
                    container.kill()
                except Exception:
                    pass
            duration_ms = int((time.monotonic() - started) * 1000)

            stdout = _decode(container.logs(stdout=True, stderr=False))
            stderr = _decode(container.logs(stdout=False, stderr=True))
            if timed_out:
                stderr += (
                    f"\n[agentspace] killed after {spec.timeout_s}s "
                    "(plan execution timeout reached)"
                )
            try:
                container.remove(force=True)
            except Exception:
                pass

            return ExecResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                timed_out=timed_out,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # -- long-running services --------------------------------------------
    async def start_service(self, spec: ServiceSpec) -> ServiceHandle:
        return await anyio.to_thread.run_sync(self._start_service_sync, spec)

    def _start_service_sync(self, spec: ServiceSpec) -> ServiceHandle:
        client = self._get_client()
        publish_locally = not _control_plane_is_containerised()
        # Docker cannot publish ports on an `internal` network, so the two modes
        # need different networks. Production never publishes and therefore
        # keeps full egress isolation; see `_ensure_network`.
        self._ensure_network(internal=not publish_locally and not self.allow_egress)

        container_name = f"as-svc-{spec.user_id[:8]}-{spec.name}"
        # Reuse of a name means a previous deploy of the same service.
        try:
            old = client.containers.get(container_name)
            old.remove(force=True)
        except Exception:
            pass

        kwargs = self._common_kwargs(spec.memory_mb, spec.cpus, spec.allow_egress)
        # Services always join the sandbox network — that is how the proxy
        # reaches them — but that network has no route to the internet.
        kwargs["network_mode"] = self.network
        if publish_locally:
            # Loopback only: the port must not become world-reachable, bypassing
            # the proxy and everything it enforces.
            kwargs["ports"] = {f"{spec.port}/tcp": ("127.0.0.1", None)}
        mount_root = (
            spec.workspace_path / spec.source_dir if spec.source_dir else spec.workspace_path
        )

        container = client.containers.create(
            self.image,
            command=["bash", "-lc", spec.command],
            name=container_name,
            working_dir="/app",
            environment={
                "HOME": "/tmp",
                "AGENTSPACE": "1",
                "PORT": str(spec.port),
                **spec.env,
            },
            volumes={str(mount_root): {"bind": "/app", "mode": "rw"}},
            labels={
                "agentspace.user": spec.user_id,
                "agentspace.kind": "service",
                "agentspace.name": spec.name,
            },
            restart_policy={"Name": "unless-stopped"},
            **kwargs,
        )
        # From here on the container exists, so every failure path must remove it
        # or a failed deploy leaves an orphan holding memory and a port.
        try:
            container.start()

            if not publish_locally:
                return ServiceHandle(
                    container_id=container.id, internal_host=container_name, port=spec.port
                )

            container.reload()
            bindings = (container.attrs.get("NetworkSettings", {}).get("Ports") or {}).get(
                f"{spec.port}/tcp"
            )
            if not bindings:
                internal = bool(
                    self._get_client().networks.get(self.network).attrs.get("Internal")
                )
                detail = (
                    f"network {self.network!r} is marked internal, and Docker cannot publish "
                    "ports on an internal network"
                    if internal
                    else "Docker reported no host binding"
                )
                raise SandboxUnavailable(
                    f"could not expose port {spec.port} for service {spec.name!r}: {detail}"
                )
            return ServiceHandle(
                container_id=container.id,
                internal_host="127.0.0.1",
                port=int(bindings[0]["HostPort"]),
            )
        except Exception:
            try:
                container.remove(force=True)
            except Exception:
                log.exception("failed to clean up container %s after a failed deploy", container.id)
            raise

    def _ensure_network(self, internal: bool) -> None:
        """Create the sandbox network, or warn if an existing one disagrees.

        An existing network is never recreated — containers may be attached to
        it — but a mismatch is worth shouting about, because the difference is
        exactly whether user code can reach the internet.
        """
        client = self._get_client()
        try:
            existing = client.networks.get(self.network)
        except Exception:
            client.networks.create(
                self.network,
                driver="bridge",
                internal=internal,
                labels={"agentspace": "sandbox"},
            )
            if not internal:
                log.warning(
                    "Sandbox network %r allows egress. Expected on a development host "
                    "(services must be reachable from the host, which requires published "
                    "ports, which Docker forbids on an internal network). NOT expected in "
                    "production, where the control plane runs in a container on this same "
                    "network and nothing is published.",
                    self.network,
                )
            return

        if bool(existing.attrs.get("Internal")) != internal:
            log.warning(
                "Sandbox network %r already exists with internal=%s but this host wants "
                "internal=%s. Remove it while no services are running to apply the change: "
                "docker network rm %s",
                self.network,
                existing.attrs.get("Internal"),
                internal,
                self.network,
            )

    async def wake_service(self, container_id: str, port: int) -> ServiceHandle:
        """Restart a container the reaper stopped.

        The port matters: an ephemeral published port is not preserved across a
        restart, so the caller has to be told the new one or every request to a
        woken service goes to a dead port.
        """
        return await anyio.to_thread.run_sync(self._wake_sync, container_id, port)

    def _wake_sync(self, container_id: str, port: int) -> ServiceHandle:
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
        except Exception as exc:
            raise SandboxUnavailable(
                f"container {container_id[:12]} no longer exists; the service must be redeployed"
            ) from exc

        container.start()
        container.reload()
        if _control_plane_is_containerised():
            return ServiceHandle(
                container_id=container.id, internal_host=container.name, port=port
            )

        bindings = (container.attrs.get("NetworkSettings", {}).get("Ports") or {}).get(
            f"{port}/tcp"
        )
        if not bindings:
            raise SandboxUnavailable(f"no host binding for port {port} after waking the container")
        return ServiceHandle(
            container_id=container.id,
            internal_host="127.0.0.1",
            port=int(bindings[0]["HostPort"]),
        )

    async def stop_service(self, container_id: str) -> None:
        await anyio.to_thread.run_sync(self._stop_sync, container_id)

    def _stop_sync(self, container_id: str) -> None:
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
        except Exception:
            return
        try:
            container.stop(timeout=10)
        except Exception:
            pass

    async def remove_service(self, container_id: str) -> None:
        await anyio.to_thread.run_sync(self._remove_sync, container_id)

    def _remove_sync(self, container_id: str) -> None:
        client = self._get_client()
        try:
            client.containers.get(container_id).remove(force=True)
        except Exception:
            return

    async def logs(self, container_id: str, tail: int = 200) -> str:
        return await anyio.to_thread.run_sync(self._logs_sync, container_id, tail)

    def _logs_sync(self, container_id: str, tail: int) -> str:
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
        except Exception:
            return "[agentspace] container not found — the service may have been removed."
        return _decode(container.logs(tail=tail, stdout=True, stderr=True))


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    if len(text) > OUTPUT_LIMIT:
        cut = len(text) - OUTPUT_LIMIT
        text = text[-OUTPUT_LIMIT:]
        text = f"[agentspace] output truncated, {cut} earlier bytes dropped\n{text}"
    return text
