"""Admission control for sandbox executions.

Without this, `run_code` starts a container the moment it is asked to, and a
burst of concurrent calls takes the box down. The fix is not a worker pool but
an admission decision: a run is let in only when its memory fits inside the
budget the machine can actually honour.

Two limits, both checked in one query:

  * a global memory budget — the real constraint on a single box
  * a per-account count — so one busy agent cannot occupy the whole budget

State lives in the `exec_slots` table rather than in process memory, because
uvicorn runs several workers and a per-process semaphore would multiply the
budget by the worker count. Slots carry an expiry, so a worker killed mid-run
frees its slot without anyone having to notice.

Every function takes the caller's session and commits through it, rather than
opening one of its own. A second connection nested inside a request's open
transaction deadlocks on SQLite, and committing before the container starts is
what releases the row lock for the duration of the run.

The limiter is deliberately approximate. Two workers can pass the check at the
same instant and both admit, overshooting by one slot each. That is acceptable:
this is a capacity guard with headroom, not a security boundary, and paying for
`SELECT FOR UPDATE` on every execution would cost more than the overshoot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ExecSlot, User
from .teaching import AgentSpaceError

log = logging.getLogger(__name__)

# Grace added to a slot's expiry beyond the run's own timeout, covering
# container start-up and teardown. A slot that outlives this was orphaned.
SLOT_GRACE_SECONDS = 30

# Polling interval while waiting for a slot, in seconds. Backs off up to the cap
# so a long queue does not turn into a busy loop against the database.
_POLL_MIN = 0.1
_POLL_MAX = 1.0


@dataclass
class Reservation:
    """A held slot. Always release it — `run_code` does so in a `finally`."""

    slot_id: str
    waited_ms: int
    queued: bool


async def _try_reserve(
    session: AsyncSession, user: User, memory_mb: int, timeout_s: int
) -> str | None:
    """Insert a slot if both limits allow it. Returns the slot id, or None."""
    settings = get_settings()
    now = datetime.now(UTC)

    # Expired rows are dead weight; clearing them here keeps the table small
    # without needing a separate sweeper.
    await session.execute(delete(ExecSlot).where(ExecSlot.expires_at <= now))

    used_mb = await session.scalar(
        select(func.coalesce(func.sum(ExecSlot.memory_mb), 0)).where(ExecSlot.expires_at > now)
    )
    mine = await session.scalar(
        select(func.count())
        .select_from(ExecSlot)
        .where(ExecSlot.user_id == user.id, ExecSlot.expires_at > now)
    )

    if (mine or 0) >= settings.max_concurrent_execs_per_user:
        await session.commit()
        return None
    if (used_mb or 0) + memory_mb > settings.exec_memory_budget_mb:
        await session.commit()
        return None

    slot = ExecSlot(
        user_id=user.id,
        memory_mb=memory_mb,
        expires_at=now + timedelta(seconds=timeout_s + SLOT_GRACE_SECONDS),
    )
    session.add(slot)
    # Commit before returning: the slot must be visible to the other workers,
    # and no lock may be held while the container runs.
    await session.commit()
    return slot.id


async def acquire(
    session: AsyncSession, user: User, memory_mb: int, timeout_s: int
) -> Reservation:
    """Wait for a slot, or raise a `AgentSpaceError` explaining the wait.

    Waiting is capped short on purpose: an agent that is told to retry handles
    it better than an HTTP connection held open for a minute.
    """
    settings = get_settings()

    # A run that cannot fit in an empty pool will never fit. Telling the caller
    # to retry would be advice that can never work, and the "pool is full"
    # wording contradicts itself when nothing is running at all.
    if memory_mb > settings.exec_memory_budget_mb:
        raise AgentSpaceError(
            "exec_budget_too_small",
            f"This plan asks for {memory_mb} MB per run, but the whole sandbox pool is "
            f"{settings.exec_memory_budget_mb} MB. No run can ever be admitted.",
            "This is a server misconfiguration, not something you did. The operator "
            f"needs EXEC_MEMORY_BUDGET_MB to be at least {memory_mb}, or the plan's "
            "memory lowered to fit.",
            status_code=503,
            details={
                "run_needs_mb": memory_mb,
                "pool_budget_mb": settings.exec_memory_budget_mb,
            },
        )

    started = time.monotonic()
    deadline = started + settings.queue_wait_seconds
    delay = _POLL_MIN
    attempts = 0

    while True:
        slot_id = await _try_reserve(session, user, memory_mb, timeout_s)
        if slot_id is not None:
            waited_ms = int((time.monotonic() - started) * 1000)
            if attempts:
                log.info("admitted user=%s after %d ms in queue", user.username, waited_ms)
            return Reservation(slot_id=slot_id, waited_ms=waited_ms, queued=attempts > 0)

        attempts += 1
        if time.monotonic() >= deadline:
            raise await _busy_error(session, user, memory_mb)

        await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 1.6, _POLL_MAX)


async def release(session: AsyncSession, slot_id: str) -> None:
    await session.execute(delete(ExecSlot).where(ExecSlot.id == slot_id))
    await session.commit()


async def _busy_error(session: AsyncSession, user: User, memory_mb: int) -> AgentSpaceError:
    """Distinguish 'you are busy' from 'the server is busy' — different fixes."""
    settings = get_settings()
    snapshot = await snapshot_state(session, user)

    if snapshot["yours"] >= settings.max_concurrent_execs_per_user:
        return AgentSpaceError(
            "exec_concurrency",
            f"You already have {snapshot['yours']} executions running, which is the limit "
            f"of {settings.max_concurrent_execs_per_user} per account.",
            "Wait for one of your own runs to finish before starting another. If you are "
            "issuing runs in parallel, serialise them — this limit exists so one account "
            "cannot crowd out the others.",
            status_code=429,
            details={"retry_after_seconds": 5, **snapshot},
        )

    return AgentSpaceError(
        "exec_capacity",
        f"The sandbox pool is full: {snapshot['used_mb']} MB of "
        f"{snapshot['budget_mb']} MB is in use and your run needs {memory_mb} MB.",
        "This is load, not a mistake on your side. Retry in a few seconds — the wait is "
        "usually brief. Nothing in your workspace was touched.",
        status_code=503,
        details={"retry_after_seconds": 10, **snapshot},
    )


async def snapshot_state(session: AsyncSession, user: User | None = None) -> dict:
    """Current occupancy, for error details, `whoami` and operators."""
    settings = get_settings()
    now = datetime.now(UTC)
    used_mb = await session.scalar(
        select(func.coalesce(func.sum(ExecSlot.memory_mb), 0)).where(ExecSlot.expires_at > now)
    )
    running = await session.scalar(
        select(func.count()).select_from(ExecSlot).where(ExecSlot.expires_at > now)
    )
    mine = 0
    if user is not None:
        mine = await session.scalar(
            select(func.count())
            .select_from(ExecSlot)
            .where(ExecSlot.user_id == user.id, ExecSlot.expires_at > now)
        )

    return {
        "used_mb": int(used_mb or 0),
        "budget_mb": settings.exec_memory_budget_mb,
        "running": int(running or 0),
        "yours": int(mine or 0),
        "your_limit": settings.max_concurrent_execs_per_user,
    }
