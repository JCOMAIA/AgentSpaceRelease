"""Integration tests for the real Docker sandbox.

Skipped automatically when the daemon or the runtime image is missing, so the
main suite still runs on a machine without Docker. Run explicitly with:

    pytest tests/test_docker_sandbox.py -v
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from app.sandbox.base import ExecSpec, SandboxUnavailable
from app.sandbox.docker_driver import DockerDriver

IMAGE = "agentspace/runtime:latest"
NETWORK = "agentspace_test_sandbox"


@pytest.fixture(scope="module")
def driver():
    d = DockerDriver(image=IMAGE, network=NETWORK, allow_egress=False)
    try:
        client = d._get_client()
        client.images.get(IMAGE)
    except SandboxUnavailable as exc:
        pytest.skip(f"Docker not available: {exc}")
    except Exception:
        pytest.skip(f"runtime image {IMAGE} not built")
    return d


@pytest.fixture
def workspace():
    path = Path(tempfile.mkdtemp(prefix="agentspace-itest-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def spec(workspace: Path, code: str, language: str = "python", **kw) -> ExecSpec:
    return ExecSpec(
        user_id="itest",
        workspace_path=workspace,
        language=language,
        code=code,
        timeout_s=kw.pop("timeout_s", 30),
        memory_mb=kw.pop("memory_mb", 256),
        cpus=kw.pop("cpus", 0.5),
        **kw,
    )


async def test_python_runs_and_returns_stdout(driver, workspace):
    result = await driver.run(spec(workspace, "print('hello from the sandbox')"))
    assert result.exit_code == 0
    assert "hello from the sandbox" in result.stdout


async def test_node_runs(driver, workspace):
    result = await driver.run(spec(workspace, "console.log(6*7)", language="node"))
    assert result.exit_code == 0
    assert "42" in result.stdout


async def test_workspace_is_mounted_and_writes_persist(driver, workspace):
    (workspace / "input.txt").write_text("from the host", encoding="utf-8")

    result = await driver.run(
        spec(
            workspace,
            "print(open('input.txt').read())\n"
            "open('output.txt','w').write('from the sandbox')",
        )
    )
    assert result.exit_code == 0, result.stderr
    assert "from the host" in result.stdout
    assert (workspace / "output.txt").read_text(encoding="utf-8") == "from the sandbox"


async def test_stderr_and_exit_code_are_reported(driver, workspace):
    result = await driver.run(spec(workspace, "import sys; sys.exit(3)"))
    assert result.exit_code == 3

    result = await driver.run(spec(workspace, "raise ValueError('boom')"))
    assert result.exit_code != 0
    assert "boom" in result.stderr


async def test_timeout_kills_the_container(driver, workspace):
    result = await driver.run(spec(workspace, "import time; time.sleep(60)", timeout_s=3))
    assert result.timed_out is True
    assert "killed after 3s" in result.stderr


async def test_no_network_access_by_default(driver, workspace):
    """The single most important sandbox property for a free public tier."""
    result = await driver.run(
        spec(
            workspace,
            "import socket\n"
            "socket.setdefaulttimeout(5)\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53))\n"
            "    print('NETWORK REACHABLE')\n"
            "except OSError as e:\n"
            "    print('blocked:', type(e).__name__)\n",
        )
    )
    assert "NETWORK REACHABLE" not in result.stdout
    assert "blocked:" in result.stdout


async def test_runs_as_non_root(driver, workspace):
    result = await driver.run(spec(workspace, "import os; print(os.getuid())", language="python"))
    assert result.stdout.strip() == "10001"


async def test_root_filesystem_is_read_only(driver, workspace):
    result = await driver.run(
        spec(
            workspace,
            "try:\n"
            "    open('/etc/evil','w').write('x')\n"
            "    print('WROTE TO ROOTFS')\n"
            "except OSError as e:\n"
            "    print('readonly:', e.errno)\n",
        )
    )
    assert "WROTE TO ROOTFS" not in result.stdout
    assert "readonly:" in result.stdout


async def test_memory_limit_is_enforced(driver, workspace):
    """Just over the ceiling, not far over.

    An allocation of several times the limit would also be refused with swap
    enabled, which is how an earlier version of this test passed while the
    container could actually reach twice its stated memory. 200 MB against a
    128 MB plan fits comfortably in the default memory+swap allowance, so this
    only passes when `memswap_limit` pins swap to zero.
    """
    result = await driver.run(
        spec(workspace, "x = bytearray(200 * 1024 * 1024); print('ALLOCATED')", memory_mb=128)
    )
    assert "ALLOCATED" not in result.stdout, "container exceeded its memory ceiling via swap"


async def test_cannot_reach_the_docker_socket(driver, workspace):
    """If this ever fails, sandbox escape is one API call away."""
    result = await driver.run(
        spec(
            workspace,
            "import os\n"
            "print('SOCKET' if os.path.exists('/var/run/docker.sock') else 'absent')",
        )
    )
    assert result.stdout.strip() == "absent"


async def test_the_full_chain_through_the_broker_runs_real_containers(driver, monkeypatch):
    """RemoteDriver -> broker -> DockerDriver -> a real container.

    The unit tests for the broker replace Docker with a recorder, so they prove
    the boundary decides correctly but not that the whole chain works. This one
    proves the production wiring end to end, including that the broker derives
    the workspace itself.
    """
    from httpx import ASGITransport, AsyncClient

    from app import broker
    from app.config import get_settings
    from app.sandbox.base import ExecSpec
    from app.sandbox.remote_driver import RemoteDriver

    token = "integration-token"
    monkeypatch.setattr(get_settings(), "sandbox_broker_token", token)
    monkeypatch.setattr(broker, "driver", lambda: driver)

    user_id = "fe" * 16
    remote = RemoteDriver(base_url="http://broker", token=token)
    remote._client = AsyncClient(
        transport=ASGITransport(app=broker.app),
        base_url="http://broker",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    try:
        result = await remote.run(
            ExecSpec(
                user_id=user_id,
                # Deliberately wrong: the broker must ignore it and use its own.
                workspace_path=Path("/definitely-not-this"),
                language="python",
                code="import os; open('proof.txt','w').write('via broker'); print(os.getuid())",
                timeout_s=30,
                memory_mb=256,
                cpus=0.5,
            )
        )
    finally:
        await remote.aclose()

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "10001"

    written = get_settings().data_root_abs / user_id / "proof.txt"
    try:
        assert written.read_text(encoding="utf-8") == "via broker"
    finally:
        shutil.rmtree(written.parent, ignore_errors=True)


async def test_output_is_truncated_not_unbounded(driver, workspace):
    result = await driver.run(
        spec(workspace, "print('x' * 2_000_000)", timeout_s=60, memory_mb=512)
    )
    assert len(result.stdout) < 400_000
    assert "truncated" in result.stdout
