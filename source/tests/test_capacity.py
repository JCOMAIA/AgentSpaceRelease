"""Admission control and the idle reaper.

Slots are inserted directly rather than by racing real executions: the point is
what the limiter decides given an occupancy, and constructing that occupancy by
hand makes the test deterministic instead of timing-dependent.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app import reaper
from app.config import get_settings
from app.db import session_scope
from app.models import Deployment, ExecSlot, User
from app.sandbox.base import ServiceHandle


@pytest.fixture(autouse=True)
async def clean_slots(client):
    """The pool is global state shared by the whole test session.

    Slots deliberately outlive a single request, so without this a test that
    fills the pool silently changes the verdict of every test after it.

    Depends on `client` only for ordering: that fixture creates the schema.
    """
    async with session_scope() as session:
        await session.execute(delete(ExecSlot))
    yield
    async with session_scope() as session:
        await session.execute(delete(ExecSlot))


@pytest.fixture
def fast_queue(monkeypatch):
    """Do not actually wait 15 seconds for a slot during tests."""
    monkeypatch.setattr(get_settings(), "queue_wait_seconds", 0)


async def _user(username: str) -> User:
    async with session_scope() as session:
        return await session.scalar(select(User).where(User.username == username))


async def _fill_slots(user_id: str, count: int, memory_mb: int = 512, ttl_seconds: int = 90):
    async with session_scope() as session:
        for _ in range(count):
            session.add(
                ExecSlot(
                    user_id=user_id,
                    memory_mb=memory_mb,
                    expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                )
            )


async def _slot_count() -> int:
    async with session_scope() as session:
        return len((await session.scalars(select(ExecSlot))).all())


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------
async def test_a_normal_run_reports_no_queue_wait(client, account):
    res = await client.post(
        "/api/v1/exec", json={"language": "python", "code": "print(1)"}, headers=account["headers"]
    )
    assert res.status_code == 200
    assert res.json()["data"]["queued_ms"] < 1000


async def test_the_slot_is_released_after_the_run(client, account):
    before = await _slot_count()
    await client.post(
        "/api/v1/exec", json={"language": "python", "code": "print(1)"}, headers=account["headers"]
    )
    assert await _slot_count() == before, "a finished run must not keep holding its slot"


async def test_the_slot_is_released_when_the_code_fails(client, account):
    before = await _slot_count()
    res = await client.post(
        "/api/v1/exec",
        json={"language": "python", "code": "raise SystemExit(9)"},
        headers=account["headers"],
    )
    assert res.status_code == 200
    assert await _slot_count() == before


async def test_per_account_concurrency_is_capped(client, account, fast_queue):
    user = await _user(account["username"])
    limit = get_settings().max_concurrent_execs_per_user
    await _fill_slots(user.id, limit)

    res = await client.post(
        "/api/v1/exec", json={"language": "python", "code": "print(1)"}, headers=account["headers"]
    )
    assert res.status_code == 429
    error = res.json()["error"]
    assert error["code"] == "exec_concurrency"
    assert "serialise" in error["fix"]
    assert error["details"]["yours"] == limit


async def test_one_account_cannot_exhaust_the_whole_budget(client, account, fast_queue):
    """The reason the per-account cap exists at all."""
    settings = get_settings()
    hog = await _user(account["username"])
    # Enough slots to swamp the budget, if the per-account cap did not stop them.
    await _fill_slots(hog.id, settings.max_concurrent_execs_per_user)

    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "bystander", "email": "bystander@example.com",
              "password": "long-enough-password"},
    )
    other = {"Authorization": f"Bearer {reg.json()['data']['api_key']}"}

    res = await client.post(
        "/api/v1/exec", json={"language": "python", "code": "print('still served')"},
        headers=other,
    )
    assert res.status_code == 200, res.text
    assert "still served" in res.json()["data"]["stdout"]


async def test_a_full_pool_is_reported_as_load_not_as_user_error(
    client, account, fast_queue, monkeypatch
):
    monkeypatch.setattr(get_settings(), "exec_memory_budget_mb", 512)
    filler = await _user(account["username"])

    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "waiting", "email": "waiting@example.com",
              "password": "long-enough-password"},
    )
    await _fill_slots(filler.id, 1, memory_mb=512)

    res = await client.post(
        "/api/v1/exec", json={"language": "python", "code": "print(1)"},
        headers={"Authorization": f"Bearer {reg.json()['data']['api_key']}"},
    )
    assert res.status_code == 503
    error = res.json()["error"]
    assert error["code"] == "exec_capacity"
    assert "not a mistake on your side" in error["fix"]
    assert error["details"]["retry_after_seconds"] > 0


async def test_a_run_larger_than_the_whole_pool_says_so(client, account, monkeypatch):
    """It must not be reported as load.

    A run needing more than the entire budget can never be admitted, so "the
    pool is full, retry shortly" is advice that can never work — and it reads as
    self-contradictory when nothing is running.
    """
    monkeypatch.setattr(get_settings(), "exec_memory_budget_mb", 128)

    res = await client.post(
        "/api/v1/exec", json={"language": "python", "code": "print(1)"},
        headers=account["headers"],
    )
    assert res.status_code == 503
    error = res.json()["error"]
    assert error["code"] == "exec_budget_too_small"
    assert "can ever be admitted" in error["message"]
    assert "EXEC_MEMORY_BUDGET_MB" in error["fix"]
    assert error["details"]["run_needs_mb"] > error["details"]["pool_budget_mb"]


async def test_every_plan_fits_inside_the_default_budget(client):
    """Ship a plan bigger than the pool and that plan can never run anything."""
    from app.config import PLANS

    budget = get_settings().exec_memory_budget_mb
    too_big = {n: p.memory_mb for n, p in PLANS.items() if p.memory_mb > budget}
    assert not too_big, (
        f"these plans cannot run at the default EXEC_MEMORY_BUDGET_MB={budget}: {too_big}"
    )


async def test_expired_slots_do_not_block_new_runs(client, account, fast_queue):
    """A worker killed mid-run must not hold its slot forever."""
    user = await _user(account["username"])
    async with session_scope() as session:
        for _ in range(get_settings().max_concurrent_execs_per_user):
            session.add(
                ExecSlot(
                    user_id=user.id,
                    memory_mb=512,
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),  # already dead
                )
            )

    res = await client.post(
        "/api/v1/exec", json={"language": "python", "code": "print('admitted')"},
        headers=account["headers"],
    )
    assert res.status_code == 200, res.text
    assert "admitted" in res.json()["data"]["stdout"]
    assert await _slot_count() == 0, "expired rows should be swept during admission"


async def test_schema_drift_is_detected_not_silently_tolerated(client):
    """`create_all` adds tables but never columns; the gap must be loud."""
    from sqlalchemy import text

    from app.db import schema_drift, session_scope

    assert await schema_drift() == [], "a freshly created schema should not drift"

    async with session_scope() as session:
        await session.execute(text("ALTER TABLE deployments DROP COLUMN last_request_at"))

    drift = await schema_drift()
    assert any("deployments" in d and "last_request_at" in d for d in drift), drift

    async with session_scope() as session:
        await session.execute(text("ALTER TABLE deployments ADD COLUMN last_request_at DATETIME"))
    assert await schema_drift() == []


async def test_whoami_exposes_pool_occupancy(client, account):
    res = await client.get("/api/v1/whoami", headers=account["headers"])
    pool = res.json()["data"]["usage"]["sandbox_pool"]
    assert pool["budget_mb"] == get_settings().exec_memory_budget_mb
    assert pool["your_limit"] == get_settings().max_concurrent_execs_per_user


# --------------------------------------------------------------------------
# Idle reaper
# --------------------------------------------------------------------------
class _FakeDriver:
    """Records what the reaper asked it to do."""

    def __init__(self, wake_port: int | None = None):
        self.stopped: list[str] = []
        self.woken: list[str] = []
        self.wake_port = wake_port

    async def stop_service(self, container_id: str) -> None:
        self.stopped.append(container_id)

    async def wake_service(self, container_id: str, port: int) -> ServiceHandle:
        self.woken.append(container_id)
        if self.wake_port is None:
            raise RuntimeError("wake not configured for this test")
        return ServiceHandle(
            container_id=container_id, internal_host="127.0.0.1", port=self.wake_port
        )


async def _make_service(username: str, name: str, *, last_request_at, status="running",
                        internal_port=1):
    user = await _user(username)
    async with session_scope() as session:
        dep = Deployment(
            user_id=user.id, name=name, kind="service", source_dir=".",
            command="irrelevant", port=8080, container_id=f"cid-{name}",
            internal_host="127.0.0.1", internal_port=internal_port,
            status=status, last_request_at=last_request_at,
        )
        session.add(dep)
        await session.flush()
        return dep.id


async def test_a_service_idle_past_the_window_is_stopped(client, account, monkeypatch):
    fake = _FakeDriver()
    monkeypatch.setattr(reaper, "get_driver", lambda: fake)
    monkeypatch.setattr(get_settings(), "service_idle_minutes", 60)

    dep_id = await _make_service(
        account["username"], "sleepy",
        last_request_at=datetime.now(UTC) - timedelta(hours=3),
    )

    assert await reaper.reap_once() == 1
    assert fake.stopped == ["cid-sleepy"]

    async with session_scope() as session:
        assert (await session.get(Deployment, dep_id)).status == "idle"


async def test_a_service_with_recent_traffic_is_left_alone(client, account, monkeypatch):
    fake = _FakeDriver()
    monkeypatch.setattr(reaper, "get_driver", lambda: fake)
    monkeypatch.setattr(get_settings(), "service_idle_minutes", 60)

    await _make_service(
        account["username"], "busy", last_request_at=datetime.now(UTC) - timedelta(minutes=5)
    )

    assert await reaper.reap_once() == 0
    assert fake.stopped == []


async def test_static_sites_are_never_reaped(client, account, monkeypatch):
    fake = _FakeDriver()
    monkeypatch.setattr(reaper, "get_driver", lambda: fake)
    monkeypatch.setattr(get_settings(), "service_idle_minutes", 60)

    await client.put("/api/v1/files?path=index.html", json={"content": "<p>x</p>"},
                     headers=account["headers"])
    await client.post("/api/v1/deployments", headers=account["headers"],
                      json={"name": "site", "kind": "static", "source_dir": "."})

    assert await reaper.reap_once() == 0


async def test_reaping_can_be_disabled(client, account, monkeypatch):
    fake = _FakeDriver()
    monkeypatch.setattr(reaper, "get_driver", lambda: fake)
    monkeypatch.setattr(get_settings(), "service_idle_minutes", 0)

    await _make_service(
        account["username"], "forever",
        last_request_at=datetime.now(UTC) - timedelta(days=30),
    )
    assert await reaper.reap_once() == 0


# --------------------------------------------------------------------------
# Waking
# --------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"awake": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


async def test_a_request_wakes_an_idle_service_and_is_served(
    client, account, monkeypatch, upstream
):
    """Idling must cost latency, not availability — the URL keeps working."""
    fake = _FakeDriver(wake_port=upstream)
    monkeypatch.setattr(reaper, "get_driver", lambda: fake)

    dep_id = await _make_service(
        account["username"], "wakeme",
        last_request_at=datetime.now(UTC) - timedelta(days=1),
        status="idle", internal_port=1,  # stale port: only a real wake can fix it
    )

    res = await client.get(f"/u/{account['username']}/wakeme/")
    assert res.status_code == 200, res.text[:300]
    assert res.json()["awake"] is True
    assert fake.woken == ["cid-wakeme"]

    async with session_scope() as session:
        dep = await session.get(Deployment, dep_id)
        assert dep.status == "running"
        # The republished port must be recorded, or the next request 404s.
        assert dep.internal_port == upstream


async def test_a_service_that_cannot_be_woken_says_so(client, account, monkeypatch):
    fake = _FakeDriver(wake_port=None)  # wake_service raises
    monkeypatch.setattr(reaper, "get_driver", lambda: fake)

    await _make_service(
        account["username"], "broken",
        last_request_at=datetime.now(UTC) - timedelta(days=1), status="idle",
    )

    with pytest.raises(RuntimeError):
        await client.get(f"/u/{account['username']}/broken/")


async def test_traffic_updates_the_last_seen_timestamp(client, account, upstream):
    dep_id = await _make_service(
        account["username"], "tracked",
        last_request_at=datetime.now(UTC) - timedelta(hours=5), internal_port=upstream,
    )

    res = await client.get(f"/u/{account['username']}/tracked/")
    assert res.status_code == 200

    async with session_scope() as session:
        dep = await session.get(Deployment, dep_id)
        last = dep.last_request_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        assert (datetime.now(UTC) - last).total_seconds() < 60


def test_no_plan_lets_one_upload_exceed_its_whole_quota():
    """A plan that allows a 200 MB upload into a 100 MB space is a broken offer.

    Caught nothing when written; it exists because the plan table was resized
    from a 32 GB box down to a 40 GB one, and the disk numbers moved by two
    orders of magnitude while the upload ceilings did not.
    """
    from app.config import PLANS

    for name, plan in PLANS.items():
        assert plan.max_upload_mb <= plan.disk_mb, (
            f"{name}: upload ceiling {plan.max_upload_mb} MB exceeds the "
            f"{plan.disk_mb} MB the plan actually grants"
        )


def test_paid_plans_offer_more_than_the_free_one():
    """Otherwise there is nothing to buy."""
    from app.config import PLANS

    free = PLANS["free"]
    for name, plan in PLANS.items():
        if plan.is_paid:
            assert plan.disk_mb > free.disk_mb, f"{name} grants no more disk than free"
            assert plan.max_upload_mb > free.max_upload_mb, f"{name} uploads no bigger"
