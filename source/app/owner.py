"""Personal mode: one owner, provisioned on first boot.

The hosted product needs registration, plans and billing. Someone running this
for their own agent needs none of it — they need a key and a URL. Personal mode
is that instance: it creates its owner the first time it starts, writes the
credentials somewhere findable, and refuses signup because there is nobody else
to sign up.

The credentials file is plaintext on the machine that already holds the
workspace and the database. That is the same trust boundary, not a new one.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ApiKey, User
from .security import hash_password, mint_api_key

log = logging.getLogger(__name__)

CREDENTIALS_FILENAME = "owner-credentials.txt"


@dataclass
class OwnerCredentials:
    username: str
    password: str
    api_key: str
    created: bool


def credentials_path() -> Path:
    """Beside the data root, so it travels with the instance it belongs to."""
    return get_settings().data_root_abs.parent / CREDENTIALS_FILENAME


async def ensure_owner(session: AsyncSession) -> OwnerCredentials | None:
    """Create the owner if this instance has none. Returns None if it already had one.

    Idempotent by design: restarting must not mint a second key or reset a
    password, or every restart would invalidate whatever the agent is holding.
    """
    settings = get_settings()
    username = settings.owner_username.strip().lower()

    existing = await session.scalar(select(User).where(User.username == username))
    if existing is not None:
        return None

    password = secrets.token_urlsafe(16)
    user = User(
        username=username,
        email=f"{username}@localhost",
        password_hash=hash_password(password),
        plan="personal",
    )
    session.add(user)
    await session.flush()

    full_key, prefix, key_hash = mint_api_key()
    session.add(ApiKey(user_id=user.id, name="owner-key", prefix=prefix, key_hash=key_hash))
    await session.flush()

    from .deps import ensure_workspace

    await ensure_workspace(session, user)

    credentials = OwnerCredentials(username, password, full_key, created=True)
    _write_credentials(credentials)
    log.info("personal mode: created owner %r and wrote %s", username, credentials_path())
    return credentials


def _write_credentials(credentials: OwnerCredentials) -> None:
    """Persist them once. The key is only ever recoverable from here."""
    settings = get_settings()
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "AgentSpace — personal instance\n"
        "==============================\n\n"
        f"Dashboard   {settings.public_url}/dashboard\n"
        f"Username    {credentials.username}\n"
        f"Password    {credentials.password}\n\n"
        f"API key     {credentials.api_key}\n\n"
        "Give the API key to your agent. Point it at\n"
        f"{settings.public_url}/api/v1/hello — that endpoint explains the rest.\n\n"
        "Only a hash of the key is stored, so this file is the one copy. Anyone\n"
        "who reads it controls this instance; it is as sensitive as the key.\n"
        "Lost it? `python -m app.cli owner key` mints a replacement.\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)  # best effort; a no-op on Windows
    except OSError:
        pass


async def mint_owner_key(session: AsyncSession, name: str = "owner-key") -> str:
    """Issue a replacement key for the owner. Old keys keep working."""
    settings = get_settings()
    username = settings.owner_username.strip().lower()
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        raise SystemExit(
            f"no owner named {username!r}. Start the server once in personal mode first."
        )
    full_key, prefix, key_hash = mint_api_key()
    session.add(ApiKey(user_id=user.id, name=name, prefix=prefix, key_hash=key_hash))
    return full_key
