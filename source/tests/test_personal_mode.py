"""Personal mode: one owner, no signup, no plans, no billing.

The hosted product needs accounts and pricing. Somebody running this for their
own agent needs a key and a URL, and every screen that asks them to choose a
plan is a screen that gets in the way.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import owner
from app.config import PLANS, get_settings
from app.db import session_scope
from app.models import ApiKey, User

_instances = {"n": 0}


@pytest.fixture
def personal(monkeypatch, tmp_path):
    """A fresh personal instance per test.

    The suite shares one database, and provisioning is idempotent by design — so
    a fixed owner name would mean only the first test ever creates one and every
    other gets `None`. A distinct name per test is a new instance, which is what
    each of these is actually about.
    """
    _instances["n"] += 1
    settings = get_settings()
    monkeypatch.setattr(settings, "single_user_mode", True)
    monkeypatch.setattr(settings, "owner_username", f"owner{_instances['n']}")
    # Keep the credentials file out of the real data directory.
    monkeypatch.setattr(settings, "data_root", tmp_path / "workspaces")
    return settings


async def provision():
    async with session_scope() as session:
        return await owner.ensure_owner(session)


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------
async def test_the_owner_is_created_on_first_boot(client, personal):
    credentials = await provision()

    assert credentials is not None
    assert credentials.username == personal.owner_username
    assert credentials.api_key.startswith("ask_")
    assert credentials.password


async def test_the_key_works_immediately(client, personal):
    credentials = await provision()

    res = await client.get(
        "/api/v1/whoami", headers={"Authorization": f"Bearer {credentials.api_key}"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["username"] == personal.owner_username


async def test_the_owner_gets_the_personal_plan(client, personal):
    credentials = await provision()

    res = await client.get(
        "/api/v1/whoami", headers={"Authorization": f"Bearer {credentials.api_key}"}
    )
    data = res.json()["data"]
    assert data["plan"] == "personal"
    # Generous, not absent: a runaway agent can still fill a disk.
    assert data["usage"]["sandbox"]["memory_mb"] == PLANS["personal"].memory_mb
    assert data["usage"]["deployments"]["limit"] == 50


async def test_restarting_does_not_mint_a_second_key(client, personal):
    """Otherwise every restart invalidates whatever the agent is holding."""
    first = await provision()
    assert first is not None

    assert await provision() is None
    assert await provision() is None

    async with session_scope() as session:
        user = await session.scalar(
            select(User).where(User.username == personal.owner_username)
        )
        keys = (await session.scalars(select(ApiKey).where(ApiKey.user_id == user.id))).all()
    assert len(keys) == 1

    res = await client.get(
        "/api/v1/whoami", headers={"Authorization": f"Bearer {first.api_key}"}
    )
    assert res.status_code == 200


async def test_the_credentials_are_written_where_they_can_be_found(client, personal):
    credentials = await provision()
    path = owner.credentials_path()

    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert credentials.api_key in body
    assert credentials.password in body
    # It must say plainly what the file is worth.
    assert "sensitive" in body.lower()


async def test_a_replacement_key_can_be_minted(client, personal):
    first = await provision()
    async with session_scope() as session:
        replacement = await owner.mint_owner_key(session)

    assert replacement.startswith("ask_")
    assert replacement != first.api_key

    for key in (first.api_key, replacement):
        res = await client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {key}"})
        assert res.status_code == 200, "old keys keep working until revoked"


# --------------------------------------------------------------------------
# No signup
# --------------------------------------------------------------------------
async def test_registration_is_refused_and_says_where_the_key_is(client, personal):
    res = await client.post(
        "/api/v1/account/register",
        json={"username": "someone", "email": "s@example.com",
              "password": "long-enough-password"},
    )
    assert res.status_code == 403
    error = res.json()["error"]
    assert error["code"] == "single_user_instance"
    assert "owner-credentials.txt" in error["fix"]


async def test_registration_still_works_when_personal_mode_is_off(client):
    res = await client.post(
        "/api/v1/account/register",
        json={"username": "hosted", "email": "hosted@example.com",
              "password": "long-enough-password"},
    )
    assert res.status_code == 200


# --------------------------------------------------------------------------
# The pages drop what does not apply
# --------------------------------------------------------------------------
async def test_the_landing_page_offers_no_signup(client, personal):
    body = (await client.get("/")).text
    assert "Get a free space" not in body
    assert "Get a space" not in body
    assert "owner-credentials.txt" in body


async def test_the_landing_page_still_sells_when_hosted(client):
    body = (await client.get("/")).text
    assert "Get a free space" in body


async def test_the_agent_facing_manual_is_unchanged(client, personal):
    """Personal mode changes the human surface, never the agent's."""
    data = (await client.get("/api/v1/hello")).json()["data"]
    assert data["doors"].keys() == {"rest", "mcp", "a2a"}
    assert len(data["capabilities"]) >= 10
