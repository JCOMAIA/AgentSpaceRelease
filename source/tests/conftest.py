"""Test fixtures.

Environment is configured before `app` is imported anywhere, because
`get_settings()` is cached for the process lifetime.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentspace-tests-"))
os.environ.update(
    {
        # Ignore the developer's own .env. Without this the suite inherits local
        # configuration — INVITE_REQUIRED, a different sandbox driver — and fails
        # in ways that have nothing to do with the change being tested.
        "AGENTSPACE_ENV_FILE": str(_TMP / "no-such.env"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}",
        "DATA_ROOT": str(_TMP / "workspaces"),
        "SANDBOX_DRIVER": "local_unsafe",
        # Otherwise the suite inherits how full the developer's disk happens to
        # be: on a machine below the production reserve, every write in every
        # test is refused for a reason that has nothing to do with the code.
        "DISK_RESERVE_MB": "1",
        "SECRET_KEY": "test-secret-key-not-for-production",
        "PUBLIC_URL": "http://testserver",
        "BASE_DOMAIN": "agentspace.test",
        "REGISTRATION_OPEN": "true",
    }
)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import ratelimit  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Counters are process-global and the whole suite shares one client address.

    Without this the limiter does its job and starves later tests of accounts,
    which looks like a product bug and is not one. Tests that exercise the
    limiter deliberately opt back in by not relying on this having run.
    """
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
async def client():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


_counter = {"n": 0}


@pytest.fixture
async def account(client):
    """Register a fresh account and return `(username, api_key, auth_headers)`."""
    _counter["n"] += 1
    n = _counter["n"]
    username = f"agent{n}"
    res = await client.post(
        "/api/v1/account/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "a-long-enough-password",
        },
    )
    assert res.status_code == 200, res.text
    key = res.json()["data"]["api_key"]
    return {
        "username": username,
        "api_key": key,
        "headers": {"Authorization": f"Bearer {key}"},
    }
