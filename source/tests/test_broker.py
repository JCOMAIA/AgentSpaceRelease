"""The privilege boundary between the control plane and the Docker daemon.

These tests are written from the attacker's side: assume the control plane is
already compromised and is now asking the broker for whatever it can get. What
it must not be able to get is a mount outside a workspace, someone else's host
path, or unbounded resources.

The Docker driver underneath is replaced with a recorder, so what is asserted is
the *specification the broker built* — which is where the decisions live.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import broker
from app.config import get_settings
from app.sandbox.base import ExecResult, ServiceHandle
from app.sandbox.remote_driver import RemoteDriver

TOKEN = "test-broker-token"


class RecordingDriver:
    """Stands in for Docker and remembers exactly what it was asked to do."""

    name = "recording"

    def __init__(self):
        self.exec_specs = []
        self.service_specs = []
        self.stopped = []
        self.removed = []

    async def is_available(self) -> bool:
        return True

    async def run(self, spec) -> ExecResult:
        self.exec_specs.append(spec)
        return ExecResult(exit_code=0, stdout="ran", stderr="", duration_ms=7)

    async def start_service(self, spec) -> ServiceHandle:
        self.service_specs.append(spec)
        return ServiceHandle(container_id="cid", internal_host="127.0.0.1", port=32768)

    async def wake_service(self, container_id, port) -> ServiceHandle:
        return ServiceHandle(container_id=container_id, internal_host="127.0.0.1", port=32769)

    async def stop_service(self, container_id) -> None:
        self.stopped.append(container_id)

    async def remove_service(self, container_id) -> None:
        self.removed.append(container_id)

    async def logs(self, container_id, tail=200) -> str:
        return f"logs for {container_id} tail={tail}"


@pytest.fixture
def recorder(monkeypatch):
    driver = RecordingDriver()
    monkeypatch.setattr(broker, "driver", lambda: driver)
    monkeypatch.setattr(get_settings(), "sandbox_broker_token", TOKEN)
    return driver


@pytest.fixture
async def broker_client(recorder):
    transport = ASGITransport(app=broker.app)
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        yield client


@pytest.fixture
async def remote(recorder):
    """A RemoteDriver wired straight to the in-process broker."""
    driver = RemoteDriver(base_url="http://broker", token=TOKEN)
    driver._client = AsyncClient(
        transport=ASGITransport(app=broker.app),
        base_url="http://broker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    yield driver
    await driver.aclose()


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
async def test_no_token_is_refused(broker_client):
    res = await broker_client.post("/run", json={"user_id": "ab" * 8, "language": "python",
                                                 "code": "print(1)"})
    assert res.status_code == 401


async def test_a_wrong_token_is_refused(broker_client):
    res = await broker_client.post(
        "/run",
        json={"user_id": "ab" * 8, "language": "python", "code": "print(1)"},
        headers=auth("not-the-token"),
    )
    assert res.status_code == 401


async def test_health_needs_no_token(broker_client):
    """Compose health checks must not need the shared secret."""
    res = await broker_client.get("/health")
    assert res.status_code == 200
    assert res.json()["available"] is True


async def test_health_says_nothing_about_the_host(broker_client):
    """It is unauthenticated, so it must not describe the daemon."""
    body = await broker_client.get("/health")
    assert set(body.json()) == {"status", "available"}


async def test_posture_needs_the_token(broker_client):
    res = await broker_client.get("/posture")
    assert res.status_code == 401


async def test_posture_reports_what_the_socket_is_worth(broker_client, monkeypatch):
    """The control plane cannot see the daemon any more; this is how it still can."""
    from app.sandbox.docker_driver import DockerDriver

    real = DockerDriver(image="i", network="n", allow_egress=False, runtime="runsc")
    monkeypatch.setattr(
        real, "_get_client", lambda: _FakeDockerClient(["name=rootless"])
    )
    monkeypatch.setattr(broker, "driver", lambda: real)

    body = (await broker_client.get("/posture", headers=auth())).json()
    assert body["rootless"] is True
    assert body["runtime"] == "runsc"
    # The fake daemon only registers runc, so the wanted runtime is missing.
    assert body["runtime_available"] is False
    assert "runc" in body["runtimes"]


# --------------------------------------------------------------------------
# Paths are derived, never accepted
# --------------------------------------------------------------------------
async def test_the_workspace_path_comes_from_the_user_id(broker_client, recorder):
    user_id = "ab" * 16
    res = await broker_client.post(
        "/run",
        json={"user_id": user_id, "language": "python", "code": "print(1)"},
        headers=auth(),
    )
    assert res.status_code == 200

    spec = recorder.exec_specs[0]
    expected = get_settings().data_root_abs / user_id
    assert spec.workspace_path == expected


async def test_a_path_in_the_request_body_is_ignored(broker_client, recorder):
    """The field does not exist; sending it must not create a mount."""
    res = await broker_client.post(
        "/run",
        json={
            "user_id": "cd" * 16,
            "language": "python",
            "code": "print(1)",
            "workspace_path": "/",
            "volumes": {"/": {"bind": "/host", "mode": "rw"}},
        },
        headers=auth(),
    )
    assert res.status_code == 200
    spec = recorder.exec_specs[0]
    assert str(spec.workspace_path).endswith("cd" * 16)
    assert "/host" not in str(spec.workspace_path)


@pytest.mark.parametrize(
    "user_id",
    ["../../etc", "/etc/passwd", "..", "not-hex!", "", "a", "ab" * 40],
)
async def test_a_user_id_that_is_not_an_id_is_refused(broker_client, user_id):
    res = await broker_client.post(
        "/run",
        json={"user_id": user_id, "language": "python", "code": "print(1)"},
        headers=auth(),
    )
    assert res.status_code == 400, f"{user_id!r} was accepted"


@pytest.mark.parametrize("source_dir", ["../../..", "/etc", "www/../../../root", "/"])
async def test_a_service_cannot_mount_outside_its_workspace(broker_client, source_dir):
    res = await broker_client.post(
        "/services",
        json={
            "user_id": "ef" * 16,
            "name": "svc",
            "source_dir": source_dir,
            "command": "python server.py",
            "port": 8080,
        },
        headers=auth(),
    )
    assert res.status_code == 400, f"{source_dir!r} was accepted"


async def test_a_service_name_must_be_a_slug(broker_client):
    res = await broker_client.post(
        "/services",
        json={"user_id": "ef" * 16, "name": "../escape", "command": "true", "port": 8080},
        headers=auth(),
    )
    assert res.status_code == 400


# --------------------------------------------------------------------------
# Resources are clamped
# --------------------------------------------------------------------------
async def test_an_enormous_memory_request_is_clamped(broker_client, recorder):
    res = await broker_client.post(
        "/run",
        json={
            "user_id": "11" * 16,
            "language": "python",
            "code": "print(1)",
            "memory_mb": 1_000_000,
            "cpus": 512,
            "timeout_s": 999_999,
        },
        headers=auth(),
    )
    assert res.status_code == 200

    settings = get_settings()
    spec = recorder.exec_specs[0]
    assert spec.memory_mb == settings.broker_max_memory_mb
    assert spec.cpus == settings.broker_max_cpus
    assert spec.timeout_s == settings.broker_max_timeout_s


async def test_a_reasonable_request_passes_through_unchanged(broker_client, recorder):
    await broker_client.post(
        "/run",
        json={
            "user_id": "22" * 16,
            "language": "python",
            "code": "print(1)",
            "memory_mb": 512,
            "cpus": 0.5,
            "timeout_s": 60,
        },
        headers=auth(),
    )
    spec = recorder.exec_specs[0]
    assert (spec.memory_mb, spec.cpus, spec.timeout_s) == (512, 0.5, 60)


async def test_a_privileged_port_is_refused(broker_client):
    res = await broker_client.post(
        "/services",
        json={"user_id": "33" * 16, "name": "svc", "command": "true", "port": 22},
        headers=auth(),
    )
    assert res.status_code == 422


# --------------------------------------------------------------------------
# The remote driver speaks the same protocol as the local one
# --------------------------------------------------------------------------
async def test_remote_driver_runs_code_through_the_broker(remote, recorder):
    from app.sandbox.base import ExecSpec

    result = await remote.run(
        ExecSpec(
            user_id="44" * 16,
            workspace_path="/ignored-by-the-broker",
            language="python",
            code="print('hi')",
            timeout_s=30,
            memory_mb=512,
            cpus=0.5,
        )
    )
    assert result.exit_code == 0
    assert result.stdout == "ran"
    # The local path was not honoured — the broker derived its own.
    assert str(recorder.exec_specs[0].workspace_path) != "/ignored-by-the-broker"


async def test_remote_driver_covers_the_service_lifecycle(remote, recorder):
    from app.sandbox.base import ServiceSpec

    handle = await remote.start_service(
        ServiceSpec(
            user_id="55" * 16,
            name="api",
            workspace_path="/ignored",
            source_dir="svc",
            command="python server.py",
            port=8080,
            memory_mb=512,
            cpus=0.5,
        )
    )
    assert handle.port == 32768

    woken = await remote.wake_service(handle.container_id, 8080)
    assert woken.port == 32769

    assert "tail=50" in await remote.logs(handle.container_id, tail=50)

    await remote.stop_service(handle.container_id)
    await remote.remove_service(handle.container_id)
    assert recorder.stopped == ["cid"]
    assert recorder.removed == ["cid"]


async def test_the_remote_driver_satisfies_the_protocol():
    """If this fails, one door works and another silently does not."""
    from app.sandbox.base import SandboxDriver
    from app.sandbox.docker_driver import DockerDriver

    for driver in (
        RemoteDriver(base_url="http://broker", token="t"),
        DockerDriver(image="i", network="n", allow_egress=False),
    ):
        assert isinstance(driver, SandboxDriver), type(driver).__name__


# --------------------------------------------------------------------------
# What holding the socket is currently worth
# --------------------------------------------------------------------------
class _FakeDockerClient:
    def __init__(self, security_options):
        self._security_options = security_options

    def info(self):
        return {"SecurityOptions": self._security_options, "Runtimes": {"runc": {}}}


@pytest.mark.parametrize(
    ("security_options", "expected"),
    [
        (["name=seccomp,profile=builtin", "name=rootless"], True),
        (["name=rootless"], True),
        (["name=seccomp,profile=builtin"], False),
        ([], False),
        (None, False),
    ],
)
def test_rootless_daemons_are_recognised(security_options, expected, monkeypatch):
    from app.sandbox.docker_driver import DockerDriver

    docker_driver = DockerDriver(image="i", network="n", allow_egress=False)
    monkeypatch.setattr(
        docker_driver, "_get_client", lambda: _FakeDockerClient(security_options)
    )
    assert docker_driver.daemon_is_rootless() is expected


async def test_a_rootful_daemon_is_refused_when_required(monkeypatch):
    """The point is that a daemon reverting to rootful cannot go unnoticed."""
    from app import broker as broker_module

    class RootfulDriver:
        def daemon_is_rootless(self):
            return False

    monkeypatch.setattr(broker_module, "driver", lambda: RootfulDriver())
    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_require_rootless", True)

    with pytest.raises(RuntimeError, match="ROOTFUL"):
        await broker_module.check_daemon_privilege(settings)


async def test_a_rootful_daemon_only_warns_by_default(monkeypatch, caplog):
    from app import broker as broker_module

    class RootfulDriver:
        def daemon_is_rootless(self):
            return False

    monkeypatch.setattr(broker_module, "driver", lambda: RootfulDriver())
    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_require_rootless", False)

    with caplog.at_level("WARNING"):
        await broker_module.check_daemon_privilege(settings)
    assert "ROOTFUL" in caplog.text


async def test_an_unreachable_broker_reads_as_an_outage(monkeypatch):
    """Not as a user error: the agent should retry, not rewrite its code."""
    from app.sandbox.base import ExecSpec, SandboxUnavailable

    driver = RemoteDriver(base_url="http://127.0.0.1:1", token="t")
    with pytest.raises(SandboxUnavailable):
        await driver.run(
            ExecSpec(
                user_id="66" * 16,
                workspace_path="/tmp",
                language="python",
                code="print(1)",
                timeout_s=1,
                memory_mb=64,
                cpus=0.1,
            )
        )
    await driver.aclose()
