"""Account removal, shared by the API and the operator CLI.

Deleting an account touches containers, the filesystem and seven tables in a
particular order. Two copies of that would drift, and the copy that drifts is
the one that leaves a container running or a workspace on disk.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import storage
from .models import (
    AgentTask,
    ApiKey,
    Deployment,
    ExecJob,
    ExecSlot,
    Subscription,
    UsageEvent,
    User,
    Workspace,
)
from .sandbox import get_driver

log = logging.getLogger(__name__)

# Order matters only in that containers go first; the rows are independent.
OWNED_MODELS = (
    Deployment,
    ApiKey,
    ExecJob,
    ExecSlot,
    AgentTask,
    UsageEvent,
    Subscription,
    Workspace,
)


async def purge_user(session: AsyncSession, user: User) -> int:
    """Erase an account completely. Returns the workspace bytes removed.

    Containers are stopped before their rows are deleted: a database row is the
    only handle we have on a container, so reversing the order strands it
    running with nothing left pointing at it.
    """
    user_id, username = user.id, user.username

    deployments = (
        await session.scalars(select(Deployment).where(Deployment.user_id == user_id))
    ).all()
    driver = get_driver()
    for deployment in deployments:
        if deployment.container_id:
            try:
                await driver.remove_service(deployment.container_id)
            except Exception:
                log.exception("could not remove container for deployment %s", deployment.id)

    for model in OWNED_MODELS:
        await session.execute(delete(model).where(model.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.flush()

    removed = storage.destroy_workspace(user_id)
    log.info("purged account %s (%d bytes of workspace removed)", username, removed)
    return removed
