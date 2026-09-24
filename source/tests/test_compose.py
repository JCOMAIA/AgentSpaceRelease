"""The deployment topology is part of the security model, so it gets tested too.

The privilege boundary between the control plane and the broker is enforced by
`docker-compose.yml`, not by Python. A stray `env_file:` there hands the most
privileged service in the stack the Stripe key — which is exactly what happened
once, and is why this file exists.

Skipped when the `docker` CLI is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Names that must never reach the broker's environment.
SECRET_MARKERS = ("SECRET_KEY", "STRIPE", "POSTGRES_PASSWORD", "DATABASE_URL")

FAKE_ENV = """\
BASE_DOMAIN=example.com
ACME_EMAIL=me@example.com
POSTGRES_PASSWORD=db-password-should-not-leak
HOST_DATA_ROOT=/srv/agentspace/workspaces
SANDBOX_BROKER_TOKEN=broker-token
SECRET_KEY=session-key-should-not-leak
STRIPE_SECRET_KEY=sk_live_should_not_leak
STRIPE_WEBHOOK_SECRET=whsec_should_not_leak
"""


@pytest.fixture(scope="module")
def compose_config(tmp_path_factory):
    """Render the compose file against a throwaway .env in a throwaway directory.

    `--project-directory` is what makes this safe: compose reads `.env` from
    there, so the developer's real one is never read, written or risked. An
    earlier version skipped whenever a real .env existed, which quietly disabled
    the test that guards the secret leak on every machine that had one.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")

    project_dir = tmp_path_factory.mktemp("compose")
    (project_dir / ".env").write_text(FAKE_ENV, encoding="ascii")

    result = subprocess.run(
        [
            "docker", "compose",
            "--project-directory", str(project_dir),
            "-f", str(ROOT / "docker-compose.yml"),
            "config", "--format", "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"docker compose config failed: {result.stderr[:200]}")
    return json.loads(result.stdout)


def _service(config, name):
    assert name in config["services"], f"service {name!r} is missing"
    return config["services"][name]


def _mounted_paths(service) -> set[str]:
    return {str(v.get("source", "")) for v in service.get("volumes") or []}


def test_only_the_broker_holds_the_docker_socket(compose_config):
    for name, service in compose_config["services"].items():
        has_socket = any("docker.sock" in path for path in _mounted_paths(service))
        if name == "broker":
            assert has_socket, "the broker is the service that is supposed to have it"
        else:
            assert not has_socket, f"{name} mounts the docker socket"


def test_the_control_plane_uses_the_remote_driver(compose_config):
    env = _service(compose_config, "app").get("environment") or {}
    assert env.get("SANDBOX_DRIVER") == "remote"
    assert env.get("SANDBOX_BROKER_URL")


def test_no_secret_reaches_the_broker(compose_config):
    """The regression this file was written for."""
    env = _service(compose_config, "broker").get("environment") or {}
    leaked = sorted(k for k in env if any(marker in k for marker in SECRET_MARKERS))
    assert leaked == [], f"the broker receives secrets it has no use for: {leaked}"


def test_the_broker_still_gets_what_it_needs(compose_config):
    env = _service(compose_config, "broker").get("environment") or {}
    for required in ("DATA_ROOT", "SANDBOX_DRIVER", "SANDBOX_IMAGE", "SANDBOX_BROKER_TOKEN"):
        assert env.get(required), f"the broker is missing {required}"


def test_the_broker_is_not_published(compose_config):
    """Publishing this port would hand the host to whoever found it."""
    assert not (_service(compose_config, "broker").get("ports") or [])


def test_the_broker_is_off_the_internet_facing_network(compose_config):
    networks = _service(compose_config, "broker").get("networks") or {}
    assert "edge" not in networks, "the broker must not sit on the network Caddy is on"


def test_the_broker_container_is_hardened(compose_config):
    broker = _service(compose_config, "broker")
    assert broker.get("read_only") is True
    assert broker.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in (broker.get("security_opt") or [])


@pytest.mark.parametrize("network", ["sandbox", "internal"])
def test_isolated_networks_have_no_route_out(compose_config, network):
    """`sandbox` carries user containers; `internal` carries broker traffic.
    Neither should be able to reach the internet, or be reached from it."""
    assert compose_config["networks"][network].get("internal") is True
