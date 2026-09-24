"""Stop service containers nobody is using, and wake them when someone is.

A free-tier service holds its full memory ceiling whether it serves a thousand
requests an hour or none at all, and most of them serve none. Reaping idle
containers is the single largest capacity win available on one box.

The bargain only works if waking is invisible. `wake()` is called from the proxy
on the first request to an idle service: the container restarts, we wait for it
to listen, and the request proceeds. The visitor sees a slow page load, not an
error — so `service_idle_minutes` is a latency trade, not an availability one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings, plan_for
from .db import session_scope
from .models import Deployment, User
from .sandbox import SandboxUnavailable, get_driver

log = logging.getLogger(__name__)

# Only record a visit if the stored timestamp is older than this, so a busy
# service costs one write a minute instead of one per request.
TOUCH_RESOLUTION_SECONDS = 60


async def touch(session: AsyncSession, deployment: Deployment) -> None:
    """Record that a service was used, cheaply."""
    now = datetime.now(UTC)
    last = deployment.last_request_at
    if last is not None:
        if last.tzinfo is None:  # SQLite hands back naive datetimes
            last = last.replace(tzinfo=UTC)
        if (now - last).total_seconds() < TOUCH_RESOLUTION_SECONDS:
            return

    deployment.last_request_at = now
    await session.execute(
        update(Deployment).where(Deployment.id == deployment.id).values(last_request_at=now)
    )
    await session.commit()


async def wake(session: AsyncSession, deployment: Deployment) -> bool:
    """Restart an idle service and wait for it to accept connections."""
    settings = get_settings()
    log.info("waking idle service %s for user %s", deployment.name, deployment.user_id)

    try:
        handle = await get_driver().wake_service(deployment.container_id, deployment.port or 8080)
    except (SandboxUnavailable, NotImplementedError) as exc:
        log.warning("could not wake %s: %s", deployment.name, exc)
        await session.execute(
            update(Deployment).where(Deployment.id == deployment.id).values(status="stopped")
        )
        await session.commit()
        return False

    deployment.internal_host = handle.internal_host
    deployment.internal_port = handle.port
    deployment.status = "running"
    await session.execute(
        update(Deployment)
        .where(Deployment.id == deployment.id)
        .values(
            internal_host=handle.internal_host,
            internal_port=handle.port,
            status="running",
            last_request_at=datetime.now(UTC),
        )
    )
    # Commit before the readiness probe: the probe can take seconds, and holding
    # a write lock for that long stalls every other request on SQLite.
    await session.commit()

    # A started container is not a listening one. Poll rather than guess, so the
    # first request after a wake succeeds instead of racing the process.
    deadline = time.monotonic() + settings.service_wake_timeout_seconds
    url = f"http://{handle.internal_host}:{handle.port}/"
    async with httpx.AsyncClient(timeout=2.0) as probe:
        while time.monotonic() < deadline:
            try:
                await probe.get(url)
                return True
            except httpx.HTTPError:
                await asyncio.sleep(0.25)

    log.warning("service %s did not start listening within the wake timeout", deployment.name)
    return True  # let the proxy try anyway; its error message is the better one


async def reap_once() -> int:
    """Stop every service with no traffic for longer than the idle window."""
    settings = get_settings()
    if settings.service_idle_minutes <= 0:
        return 0

    reaped = 0
    now = datetime.now(UTC)

    async with session_scope() as session:
        candidates = (
            await session.scalars(
                select(Deployment).where(
                    Deployment.kind == "service", Deployment.status == "running"
                )
            )
        ).all()
        # Sleeping is a plan feature, not a global policy: paying for Pro buys a
        # service that stays warm, so its idle window is read per owner.
        owners = {
            user.id: user
            for user in (
                await session.scalars(
                    select(User).where(User.id.in_({d.user_id for d in candidates}))
                )
            ).all()
        } if candidates else {}

    for deployment in candidates:
        owner = owners.get(deployment.user_id)
        idle_minutes = settings.service_idle_minutes
        if owner is not None:
            plan_window = plan_for(owner.plan).idle_minutes
            if plan_window is not None:
                idle_minutes = plan_window
        if idle_minutes <= 0:
            continue  # this plan never sleeps

        cutoff = now - timedelta(minutes=idle_minutes)
        last = deployment.last_request_at or deployment.created_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last is not None and last > cutoff:
            continue

        try:
            await get_driver().stop_service(deployment.container_id)
        except Exception:
            log.exception("failed to stop idle service %s", deployment.name)
            continue

        async with session_scope() as session:
            await session.execute(
                update(Deployment).where(Deployment.id == deployment.id).values(status="idle")
            )
        reaped += 1
        log.info("reaped idle service %s (last seen %s)", deployment.name, last)

    return reaped


async def run_forever() -> None:
    """Background loop, started from the app lifespan."""
    settings = get_settings()
    if settings.service_idle_minutes <= 0:
        log.info("idle reaper disabled (service_idle_minutes=0)")
        return

    log.info(
        "idle reaper running every %ss, stopping services idle for %s minutes",
        settings.reaper_interval_seconds,
        settings.service_idle_minutes,
    )
    while True:
        try:
            await asyncio.sleep(settings.reaper_interval_seconds)
            count = await reap_once()
            if count:
                log.info("reaped %d idle service(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failure here must never end the loop; the next pass will retry.
            log.exception("reaper pass failed")
