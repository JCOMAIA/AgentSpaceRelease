"""Authentication dependencies.

Agents authenticate with `Authorization: Bearer ask_...`; humans on the
dashboard carry a signed session cookie. Both resolve to the same `User`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .models import ApiKey, User, Workspace
from .security import hash_api_key, looks_like_api_key, read_session
from .teaching import AgentSpaceError

__all__ = [
    "SESSION_COOKIE",
    "bearer_token",
    "current_user",
    "ensure_not_suspended",
    "ensure_workspace",
    "optional_user",
    "resolve_api_key",
]

SESSION_COOKIE = "agentspace_session"


def _unauthenticated() -> AgentSpaceError:
    base = get_settings().public_url
    return AgentSpaceError(
        "unauthenticated",
        "This space is private. I could not find a valid credential on your request.",
        "Send your API key as the header `Authorization: Bearer ask_...`. "
        f"If you do not have one, a human can create it at {base}/dashboard.",
        status_code=401,
        try_this={
            "transport": "rest",
            "method": "GET",
            "path": "/api/v1/hello",
            "note": "That endpoint is public and explains the whole system.",
        },
    )


# Header names a client might put the key under. A hosted connector's setup
# dialog offers a long menu of these and the person picks one; `api-key` and
# `x-api-key` are the same intention spelled two ways, and refusing a valid key
# because of the spelling is a puzzle with no lesson in it. Tried in order.
KEY_HEADERS = ("x-api-key", "api-key", "apikey", "x-agentspace-key")


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()

    for name in KEY_HEADERS:
        value = request.headers.get(name)
        if value:
            return value.strip()

    # Some clients put the key straight into Authorization with no scheme.
    if looks_like_api_key(header.strip()):
        return header.strip()
    return None


def auth_headers_seen(request: Request) -> list[str]:
    """Which credential-shaped headers arrived, for diagnosing a failed connect.

    Names only — never values. An operator watching the log after someone says
    "I pasted the key and it did not work" needs to know where the key landed,
    and that question took a round trip to answer the first time.
    """
    candidates = ("authorization", *KEY_HEADERS)
    return [name for name in candidates if request.headers.get(name)]


async def resolve_api_key(session: AsyncSession, token: str) -> User | None:
    """Resolve a key to its owner. Returns suspended users too.

    Suspension is deliberately not folded in here: an agent told "invalid API
    key" when the account was actually suspended will go looking for a
    credential problem that does not exist. Callers use `ensure_not_suspended`
    to say the true thing.
    """
    key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
    if key is None or not key.is_active:
        return None
    key.last_used_at = datetime.now(UTC)
    return key.user


def ensure_not_suspended(user: User) -> None:
    if user.is_active:
        return
    reason = user.suspended_reason or "No reason was recorded."
    raise AgentSpaceError(
        "account_suspended",
        f"The account {user.username!r} is suspended, so this key will not work.",
        "This is not a credential problem — a new key would behave the same. "
        f"The account owner needs to contact the operator. Reason: {reason}",
        status_code=403,
        details={"username": user.username, "reason": reason},
    )


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    token = bearer_token(request)
    if token:
        user = await resolve_api_key(session, token)
        if user is None:
            raise AgentSpaceError(
                "invalid_api_key",
                "That API key is not valid, or it has been revoked.",
                "Ask the account owner for a current key from the dashboard. "
                "Keys look like `ask_<prefix>_<secret>`.",
                status_code=401,
            )
        ensure_not_suspended(user)
        await ensure_workspace(session, user)
        return user

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        user_id = read_session(cookie)
        if user_id:
            user = await session.get(User, user_id)
            if user:
                ensure_not_suspended(user)
                await ensure_workspace(session, user)
                return user

    raise _unauthenticated()


async def optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | None:
    try:
        return await current_user(request, session)
    except AgentSpaceError:
        return None


async def ensure_workspace(session: AsyncSession, user: User) -> Workspace:
    """Workspaces are created lazily so an account is usable the moment it exists."""
    from . import storage

    workspace = await session.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if workspace is None:
        workspace = Workspace(user_id=user.id)
        session.add(workspace)
        await session.flush()
    storage.workspace_root(user.id)
    return workspace
