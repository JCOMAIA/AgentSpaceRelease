"""Account lifecycle: registration, login, API keys, custom domains.

These are the human-facing endpoints. Agents never call them — an agent that
could mint its own credentials would sit outside anyone's quota.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import quotas, ratelimit
from ..accounts import purge_user
from ..config import get_settings, plan_for
from ..db import get_session
from ..deps import SESSION_COOKIE, current_user, ensure_workspace
from ..models import ApiKey, Invite, User
from ..operations import space_urls
from ..security import hash_password, mint_api_key, sign_session, verify_password
from ..teaching import AgentSpaceError, Guide, NextStep, ok

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/account", tags=["account"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(current_user)]

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,37}[a-z0-9]$")  # 2-39 chars

# Paths the app itself serves. A username may not shadow one of these, because
# usernames become both a URL segment and a subdomain.
RESERVED = {
    "api", "mcp", "a2a", "u", "static", "assets", "dashboard", "register", "login",
    "logout", "docs", "redoc", "openapi", "well-known", "admin", "www", "app",
    "help", "support", "status", "blog", "about", "terms", "privacy", "llms",
    "health", "metrics", "internal", "root", "system", "agentspace",
}


class RegisterBody(BaseModel):
    username: str = Field(min_length=2, max_length=39)
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    invite: str | None = Field(default=None, description="Required while the beta is closed.")


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class ApiKeyBody(BaseModel):
    name: str = Field(default="default", max_length=64)


class CustomDomainBody(BaseModel):
    domain: str | None = Field(default=None, description="null removes the current domain")


class DeleteAccountBody(BaseModel):
    password: str = Field(description="The account password. Proves a human is asking.")
    confirm: str = Field(description="The username, typed again, to confirm intent.")


def validate_username(username: str) -> str:
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise AgentSpaceError(
            "invalid_username",
            f"{username!r} is not a valid username.",
            "Use 2-39 lowercase letters, digits or hyphens, starting and ending "
            "with a letter or digit.",
        )
    if username in RESERVED:
        raise AgentSpaceError(
            "reserved_username",
            f"{username!r} is reserved by the platform.",
            "Pick another name — it becomes part of your public URL.",
            status_code=409,
        )
    return username


async def _claim_invite(session: AsyncSession, code: str | None) -> Invite:
    """Take a single-use code out of circulation, or explain why it will not work."""
    cleaned = (code or "").strip().upper()
    if not cleaned:
        raise AgentSpaceError(
            "invite_required",
            "This instance is in closed beta, so registration needs an invite code.",
            "Ask the operator for a code and send it in the `invite` field.",
            status_code=403,
        )

    invite = await session.get(Invite, cleaned)
    if invite is None:
        raise AgentSpaceError(
            "invite_invalid",
            "That invite code does not exist.",
            "Check for typos — codes are not case-sensitive but every character counts.",
            status_code=403,
        )
    if invite.is_used:
        raise AgentSpaceError(
            "invite_used",
            "That invite code has already been used.",
            "Each code works once. Ask the operator for a fresh one.",
            status_code=403,
        )
    expires_at = invite.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:  # SQLite hands back naive datetimes
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            raise AgentSpaceError(
                "invite_expired",
                "That invite code has expired.",
                "Ask the operator for a new one.",
                status_code=403,
            )
    return invite


async def _create_user(session: AsyncSession, body: RegisterBody) -> User:
    settings = get_settings()
    if settings.single_user_mode:
        raise AgentSpaceError(
            "single_user_instance",
            "This is a personal instance. It has one owner and no signup.",
            "The owner's credentials were written to a file when the server first "
            "started — look for owner-credentials.txt next to the data directory. "
            "To mint another key: python -m app.cli owner key",
            status_code=403,
        )
    if not settings.registration_open:
        raise AgentSpaceError(
            "registration_closed",
            "This instance is not accepting new accounts.",
            "Contact the operator for an invite.",
            status_code=403,
        )
    invite = await _claim_invite(session, body.invite) if settings.invite_required else None
    username = validate_username(body.username)

    if await session.scalar(select(User).where(User.username == username)):
        raise AgentSpaceError(
            "username_taken", f"{username!r} is already in use.",
            "Choose a different username.", status_code=409,
        )
    if await session.scalar(select(User).where(User.email == str(body.email).lower())):
        raise AgentSpaceError(
            "email_taken", "That email already has an account.",
            "Log in instead, or use a different address.", status_code=409,
        )

    user = User(
        username=username,
        email=str(body.email).lower(),
        password_hash=hash_password(body.password),
        plan=settings.default_plan,
    )
    session.add(user)
    await session.flush()
    # Burn the code only once the account exists, so a rejected registration
    # does not consume someone's only invite.
    if invite is not None:
        invite.used_by_user_id = user.id
        invite.used_at = datetime.now(UTC)
    await ensure_workspace(session, user)
    return user


@router.post("/register", summary="Create an account")
async def register(
    request: Request, session: SessionDep, body: RegisterBody, response: Response
) -> dict[str, Any]:
    # A free tier is free compute; without this one host can mint accounts all day.
    ratelimit.enforce(
        "register",
        ratelimit.client_ip(request),
        ratelimit.REGISTER_PER_IP,
        what="accounts created from this address",
        fix="Wait for the window to pass. If you genuinely need several accounts, "
        "ask the operator instead of scripting registration.",
    )
    user = await _create_user(session, body)
    # Hand over a first key immediately — an account without one is inert.
    full_key, prefix, key_hash = mint_api_key()
    session.add(ApiKey(user_id=user.id, name="first-key", prefix=prefix, key_hash=key_hash))
    # Committed here rather than left to the dependency teardown, which runs
    # after the response is on its way. A caller fast enough to use the key
    # before that lands — an agent, which is the normal caller here — gets
    # `invalid_api_key` for a key we just issued. The window is widest on a
    # brand-new database, where the first commit also builds the WAL, so the
    # very first account on a fresh deployment is the one most likely to hit it.
    await session.commit()

    response.set_cookie(
        SESSION_COOKIE, sign_session(user.id),
        httponly=True, samesite="lax", secure=get_settings().public_url.startswith("https"),
        max_age=60 * 60 * 24 * 14,
    )
    s = get_settings()
    data = {
        "username": user.username,
        "plan": user.plan,
        "api_key": full_key,
        "urls": space_urls(user),
        "endpoints": {
            "rest": f"{s.public_url}/api/v1",
            "mcp": f"{s.public_url}/mcp",
            "a2a_agent_card": f"{s.public_url}/.well-known/agent-card.json",
        },
    }
    return ok(
        data,
        Guide(
            you_are_here=f"Account {user.username!r} created and its first API key issued.",
            next_steps=[
                NextStep(
                    "Give this key to your agent",
                    "It is shown once and never again.",
                    {"transport": "human", "action": "copy api_key into your agent's config"},
                ),
                NextStep(
                    "Have the agent introduce itself to the space",
                    "First contact returns the full manual.",
                    {"transport": "rest", "method": "GET", "path": "/api/v1/hello"},
                ),
            ],
            notes=["Store the key now — we only keep a hash of it."],
        ),
    )


@router.post("/login", summary="Exchange email + password for a session cookie")
async def login(
    request: Request, session: SessionDep, body: LoginBody, response: Response
) -> dict[str, Any]:
    email = str(body.email).lower()
    # Per-IP stops one host churning passwords; per-account stops a botnet
    # spreading the same attack across many hosts. Both are needed.
    ratelimit.enforce(
        "login-ip",
        ratelimit.client_ip(request),
        ratelimit.LOGIN_PER_IP,
        what="sign-in attempts from this address",
        fix="Wait for the window to pass before trying again.",
    )
    ratelimit.enforce(
        "login-account",
        email,
        ratelimit.LOGIN_PER_ACCOUNT,
        what="failed sign-ins for this account",
        fix="Wait for the window to pass, or reset the password.",
    )

    user = await session.scalar(select(User).where(User.email == email))
    # Same error either way — do not confirm whether an address is registered.
    if user is None or not verify_password(body.password, user.password_hash):
        raise AgentSpaceError(
            "invalid_credentials", "Email or password is incorrect.",
            "Check both and try again.", status_code=401,
        )
    response.set_cookie(
        SESSION_COOKIE, sign_session(user.id),
        httponly=True, samesite="lax", secure=get_settings().public_url.startswith("https"),
        max_age=60 * 60 * 24 * 14,
    )
    return ok({"username": user.username, "plan": user.plan})


@router.post("/logout", summary="Clear the session cookie")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE)
    return ok({"logged_out": True})


@router.get("/keys", summary="List your API keys (metadata only)")
async def list_keys(session: SessionDep, user: UserDep) -> dict[str, Any]:
    keys = (
        await session.scalars(
            select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
        )
    ).all()
    return ok(
        {
            "keys": [
                {
                    "id": k.id,
                    "name": k.name,
                    "prefix": k.prefix,
                    "created_at": k.created_at.isoformat(),
                    "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                    "active": k.is_active,
                }
                for k in keys
            ],
            "limit": plan_for(user.plan).max_api_keys,
        }
    )


@router.post("/keys", summary="Mint a new API key")
async def create_key(session: SessionDep, user: UserDep, body: ApiKeyBody) -> dict[str, Any]:
    await quotas.check_api_keys(session, user)
    full_key, prefix, key_hash = mint_api_key()
    key = ApiKey(user_id=user.id, name=body.name, prefix=prefix, key_hash=key_hash)
    session.add(key)
    # Same reason as register: the key has to be durable before its owner is
    # told it exists, or the first request made with it can outrun the commit.
    await session.commit()
    return ok(
        {"id": key.id, "name": key.name, "api_key": full_key},
        Guide(
            you_are_here="New key issued.",
            next_steps=[],
            notes=["This is the only time the full key is returned."],
        ),
    )


@router.delete("/keys/{key_id}", summary="Revoke an API key")
async def revoke_key(session: SessionDep, user: UserDep, key_id: str) -> dict[str, Any]:
    key = await session.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        raise AgentSpaceError(
            "key_not_found", "No such key on this account.",
            "List your keys to see valid ids.", status_code=404,
        )
    key.revoked_at = datetime.now(UTC)
    return ok({"id": key_id, "revoked": True})


@router.delete("", summary="Delete the account and everything in it")
async def delete_account(
    session: SessionDep, user: UserDep, body: DeleteAccountBody
) -> dict[str, Any]:
    """Erase the account for real: containers, files and rows.

    The password is the gate rather than the session or API key, so an agent
    holding a key cannot destroy the account it was lent access to. There is no
    soft-delete and no grace period — a data-deletion request that leaves the
    data on disk is not a deletion.
    """
    if not verify_password(body.password, user.password_hash):
        raise AgentSpaceError(
            "invalid_credentials",
            "That password is not correct, so nothing was deleted.",
            "Send the account's current password in the `password` field.",
            status_code=401,
        )
    if body.confirm != user.username:
        raise AgentSpaceError(
            "confirmation_mismatch",
            "The confirmation did not match the username.",
            f"Set `confirm` to {user.username!r} to prove this is deliberate. "
            "This action cannot be undone.",
        )

    username = user.username
    removed_bytes = await purge_user(session, user)

    return ok(
        {"deleted": True, "username": username, "workspace_bytes_removed": removed_bytes},
        Guide(
            you_are_here=f"Account {username!r} and all of its data are gone.",
            next_steps=[],
            notes=[
                "Any API key issued to this account stopped working immediately.",
                "Off-site backups may retain a copy until they rotate; ask the operator "
                "if you need those purged too.",
            ],
        ),
    )


@router.put("/domain", summary="Attach or remove a custom domain")
async def set_custom_domain(
    session: SessionDep, user: UserDep, body: CustomDomainBody
) -> dict[str, Any]:
    plan = plan_for(user.plan)
    if body.domain and not plan.allow_custom_domain:
        raise AgentSpaceError(
            "plan_forbids_custom_domain",
            f"The {plan.name} plan does not include custom domains.",
            # The path URL is the one that always exists; the subdomain form is
            # only present where wildcard DNS was configured for it.
            f"Use your included URL {space_urls(user)['path']}, or upgrade.",
            status_code=402,
        )
    domain = (body.domain or "").strip().lower().rstrip(".") or None
    if domain:
        if not re.match(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.[a-z0-9-]{1,63})+$", domain):
            raise AgentSpaceError(
                "invalid_domain", f"{domain!r} does not look like a hostname.",
                "Send a bare hostname such as 'example.com' — no scheme, no path.",
            )
        clash = await session.scalar(select(User).where(User.custom_domain == domain))
        if clash and clash.id != user.id:
            raise AgentSpaceError(
                "domain_taken", "That domain is already attached to another account.",
                "Use a domain you control, or remove it from the other account first.",
                status_code=409,
            )
    user.custom_domain = domain
    s = get_settings()
    return ok(
        {"custom_domain": domain, "urls": space_urls(user)},
        Guide(
            you_are_here=(
                f"Custom domain set to {domain!r}." if domain else "Custom domain removed."
            ),
            next_steps=[],
            notes=(
                [f"Point a CNAME from {domain} to {s.base_domain}. "
                 "TLS is issued automatically on first request."]
                if domain else []
            ),
        ),
    )
