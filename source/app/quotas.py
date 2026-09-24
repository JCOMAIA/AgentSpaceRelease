"""Plan limit enforcement.

Checks raise `AgentSpaceError`, so a quota rejection reaches the agent as an
explanation with a fix rather than a bare 403.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import storage
from .config import Plan, get_settings, plan_for
from .models import ApiKey, Deployment, UsageEvent, User
from .teaching import AgentSpaceError

log = logging.getLogger(__name__)


def plan_of(user: User) -> Plan:
    return plan_for(user.plan)


async def check_host_disk(incoming_bytes: int = 0) -> None:
    """Refuse writes before the host disk fills.

    Per-user quotas cap one workspace; nothing caps their sum. A hundred free
    accounts at 1 GB each overrun a 75 GB disk between them, and a disk at 100%
    does not merely reject writes — it takes SQLite down with it and the whole
    space stops answering. So the host gets the last word, above every plan.
    """
    settings = get_settings()
    reserve = settings.disk_reserve_mb * 1024 * 1024
    try:
        free = shutil.disk_usage(settings.data_root_abs).free
    except OSError:  # pragma: no cover - the path is created at startup
        return

    if free - incoming_bytes < reserve:
        log.error(
            "host disk near capacity: %.1f GB free, reserve is %.1f GB — writes refused",
            free / 1e9, reserve / 1e9,
        )
        raise AgentSpaceError(
            "host_disk_full",
            "This space is out of disk and cannot accept new files right now.",
            "Nothing you did caused this and retrying will not help. Delete files you no "
            "longer need, and tell the operator — they have to add space.",
            status_code=507,
            details={"free_bytes": free, "reserve_bytes": reserve},
        )


async def check_disk(user: User, incoming_bytes: int = 0) -> None:
    plan = plan_of(user)
    limit = plan.disk_mb * 1024 * 1024
    used = storage.disk_usage(user.id)
    if used + incoming_bytes > limit:
        raise AgentSpaceError(
            "quota_disk",
            f"Workspace is {used / 1e6:.1f} MB of {plan.disk_mb} MB allowed.",
            "Delete files you no longer need, then retry.",
            status_code=413,
            try_this={"transport": "rest", "method": "GET", "path": "/api/v1/files?path=."},
            details={"used_bytes": used, "limit_bytes": limit},
        )


async def check_deployments(session: AsyncSession, user: User) -> None:
    plan = plan_of(user)
    count = await session.scalar(
        select(func.count()).select_from(Deployment).where(Deployment.user_id == user.id)
    )
    if (count or 0) >= plan.max_deployments:
        raise AgentSpaceError(
            "quota_deployments",
            f"You already have {count} deployments; the {plan.name} plan allows "
            f"{plan.max_deployments}.",
            "Delete an existing deployment before creating another.",
            status_code=409,
            try_this={"transport": "rest", "method": "GET", "path": "/api/v1/deployments"},
        )


async def check_api_keys(session: AsyncSession, user: User) -> None:
    plan = plan_of(user)
    count = await session.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
    )
    if (count or 0) >= plan.max_api_keys:
        raise AgentSpaceError(
            "quota_api_keys",
            f"You have {count} active keys; the {plan.name} plan allows {plan.max_api_keys}.",
            "Revoke an unused key first.",
            status_code=409,
        )


async def check_exec_budget(session: AsyncSession, user: User) -> None:
    plan = plan_of(user)
    since = datetime.now(UTC) - timedelta(days=30)
    spent = await session.scalar(
        select(func.coalesce(func.sum(UsageEvent.amount), 0.0)).where(
            UsageEvent.user_id == user.id,
            UsageEvent.kind == "exec_seconds",
            UsageEvent.created_at >= since,
        )
    )
    if (spent or 0.0) >= plan.monthly_exec_seconds:
        raise AgentSpaceError(
            "quota_exec",
            f"You used {spent:.0f}s of compute in the last 30 days; the {plan.name} plan "
            f"allows {plan.monthly_exec_seconds}s.",
            "Wait for the rolling window to free up, or upgrade the plan.",
            status_code=429,
        )


async def record_usage(session: AsyncSession, user: User, kind: str, amount: float) -> None:
    session.add(UsageEvent(user_id=user.id, kind=kind, amount=amount))


async def usage_summary(session: AsyncSession, user: User) -> dict:
    plan = plan_of(user)
    since = datetime.now(UTC) - timedelta(days=30)
    exec_seconds = await session.scalar(
        select(func.coalesce(func.sum(UsageEvent.amount), 0.0)).where(
            UsageEvent.user_id == user.id,
            UsageEvent.kind == "exec_seconds",
            UsageEvent.created_at >= since,
        )
    )
    deployments = await session.scalar(
        select(func.count()).select_from(Deployment).where(Deployment.user_id == user.id)
    )
    used = storage.disk_usage(user.id)
    return {
        "plan": plan.name,
        "disk": {"used_bytes": used, "limit_bytes": plan.disk_mb * 1024 * 1024},
        "deployments": {"used": deployments or 0, "limit": plan.max_deployments},
        "exec_seconds_30d": {
            "used": round(exec_seconds or 0.0, 1),
            "limit": plan.monthly_exec_seconds,
        },
        "sandbox": {
            "memory_mb": plan.memory_mb,
            "cpus": plan.cpus,
            "timeout_s": plan.exec_timeout_s,
        },
    }
