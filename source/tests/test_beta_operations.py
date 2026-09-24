"""Closed-beta invites, suspension, and the operator commands.

What a beta needs that an open service does not: a way to let specific people
in, a way to remove one without deleting them, and a way to rescue someone who
forgot their password while there is still no email.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import cli, ratelimit
from app.config import get_settings
from app.db import session_scope
from app.models import Invite, User


@pytest.fixture
def invite_only(monkeypatch):
    monkeypatch.setattr(get_settings(), "invite_required", True)


async def register(client, username: str, invite: str | None = None):
    body = {
        "username": username,
        "email": f"{username}@example.com",
        "password": "long-enough-password",
    }
    if invite is not None:
        body["invite"] = invite
    return await client.post("/api/v1/account/register", json=body)


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------
async def test_registration_is_open_without_the_flag(client):
    assert (await register(client, "walkin")).status_code == 200


async def test_an_invite_is_required_when_the_beta_is_closed(client, invite_only):
    res = await register(client, "uninvited")
    assert res.status_code == 403
    error = res.json()["error"]
    assert error["code"] == "invite_required"
    assert "invite" in error["fix"]


async def test_a_valid_code_lets_someone_in(client, invite_only):
    code = (await cli.create_invites(note="first tester"))[0]
    res = await register(client, "invited", invite=code)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["api_key"].startswith("ask_")


async def test_a_code_works_exactly_once(client, invite_only):
    code = (await cli.create_invites())[0]
    assert (await register(client, "firstuse", invite=code)).status_code == 200

    res = await register(client, "seconduse", invite=code)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "invite_used"


async def test_codes_are_not_case_sensitive(client, invite_only):
    """They get read aloud and typed by hand."""
    code = (await cli.create_invites())[0]
    assert (await register(client, "lowercase", invite=code.lower())).status_code == 200


async def test_an_unknown_code_is_refused(client, invite_only):
    res = await register(client, "guesser", invite="XXXX-XXXX-XXXX")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "invite_invalid"


async def test_an_expired_code_is_refused(client, invite_only):
    async with session_scope() as session:
        session.add(
            Invite(code="OLDC-ODE1-2345", expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    res = await register(client, "latecomer", invite="OLDC-ODE1-2345")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "invite_expired"


async def test_a_failed_registration_does_not_burn_the_code(client, account, invite_only):
    """Otherwise a typo in a username costs the tester their only invite.

    `account` is requested before `invite_only` on purpose: it needs to register
    while the door is still open, so it can supply the clashing username.
    """
    code = (await cli.create_invites())[0]

    clash = await register(client, account["username"], invite=code)
    assert clash.status_code == 409  # username taken

    assert (await register(client, "recovered", invite=code)).status_code == 200


async def test_used_codes_record_who_used_them(client, invite_only):
    code = (await cli.create_invites(note="for ada"))[0]
    await register(client, "ada2", invite=code)

    used = [i for i in await cli.list_invites(include_used=True) if i["code"] == code]
    assert used and used[0]["used_by"] == "ada2"
    assert used[0]["note"] == "for ada"


async def test_unused_codes_are_listed_and_revocable(client):
    code = (await cli.create_invites(note="mistake"))[0]
    assert any(i["code"] == code for i in await cli.list_invites())

    assert await cli.revoke_invite(code) is True
    assert not any(i["code"] == code for i in await cli.list_invites())
    assert await cli.revoke_invite(code) is False


# --------------------------------------------------------------------------
# Suspension
# --------------------------------------------------------------------------
async def test_a_suspended_account_is_told_why_not_that_its_key_is_bad(client, account):
    """The distinction matters: an agent told 'invalid key' hunts a credential
    problem that does not exist."""
    await cli.suspend_user(account["username"], reason="running a crypto miner")

    res = await client.get("/api/v1/whoami", headers=account["headers"])
    assert res.status_code == 403
    error = res.json()["error"]
    assert error["code"] == "account_suspended"
    assert "crypto miner" in error["fix"]
    assert "not a credential problem" in error["fix"]


@pytest.mark.parametrize("door", ["rest", "mcp", "a2a"])
async def test_suspension_closes_every_door(client, account, door):
    await cli.suspend_user(account["username"], reason="abuse")
    headers = account["headers"]

    if door == "rest":
        res = await client.get("/api/v1/files?path=.", headers=headers)
        assert res.json()["error"]["code"] == "account_suspended"
    elif door == "mcp":
        res = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "whoami", "arguments": {}}},
            headers=headers,
        )
        result = res.json()["result"]
        assert result["isError"] is True
        assert "account_suspended" in result["content"][0]["text"]
    else:
        res = await client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                  "params": {"message": {"role": "user", "messageId": "m1",
                                         "parts": [{"kind": "text", "text": "whoami"}]}}},
            headers=headers,
        )
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "account_suspended"


async def test_a_suspended_site_stops_being_served(client, account):
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>hi</p>"},
                     headers=account["headers"])
    await client.post("/api/v1/deployments", headers=account["headers"],
                      json={"name": "site", "kind": "static", "source_dir": "."})
    assert (await client.get(f"/u/{account['username']}/")).status_code == 200

    await cli.suspend_user(account["username"], reason="dmca")
    assert (await client.get(f"/u/{account['username']}/")).status_code == 404


async def test_restoring_brings_everything_back(client, account):
    await cli.suspend_user(account["username"], reason="misunderstanding")
    assert (await client.get("/api/v1/whoami", headers=account["headers"])).status_code == 403

    await cli.restore_user(account["username"])
    res = await client.get("/api/v1/whoami", headers=account["headers"])
    assert res.status_code == 200

    async with session_scope() as session:
        user = await session.scalar(
            select(User).where(User.username == account["username"])
        )
        assert user.suspended_reason is None


# --------------------------------------------------------------------------
# Operator commands
# --------------------------------------------------------------------------
async def test_a_password_reset_actually_lets_them_back_in(client, account):
    """There is no email yet, so this is the only way out of a lockout."""
    ratelimit.reset()
    email = f"{account['username']}@example.com"
    assert (
        await client.post("/api/v1/account/login",
                          json={"email": email, "password": "wrong"})
    ).status_code == 401

    new_password = await cli.reset_password(account["username"])
    res = await client.post(
        "/api/v1/account/login", json={"email": email, "password": new_password}
    )
    assert res.status_code == 200, res.text


async def test_user_list_reports_what_an_operator_needs(client, account):
    rows = {row["username"]: row for row in await cli.list_users()}
    row = rows[account["username"]]
    assert row["plan"] == "free"
    assert row["active"] is True
    assert row["keys"] >= 1
    assert "disk" in row


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (900, "900 B"), (13_000, "12.7 KB"), (5_000_000, "4.8 MB"),
     (3_000_000_000, "2.8 GB")],
)
def test_sizes_read_as_sizes_not_as_zero(size, expected):
    """"0.0 MB" for a 13 KB workspace reads as empty when it means small."""
    assert cli.human_bytes(size) == expected


async def test_a_plan_can_be_granted_by_hand(client, account):
    result = await cli.set_plan(account["username"], "pro")
    assert (result["from"], result["to"]) == ("free", "pro")
    # Nothing in Stripe, so nothing will overwrite it.
    assert result["has_subscription"] is False

    res = await client.get("/api/v1/whoami", headers=account["headers"])
    assert res.json()["data"]["plan"] == "pro"
    assert res.json()["data"]["usage"]["sandbox"]["memory_mb"] == 4096


async def test_deleting_an_account_removes_it_everywhere(client, account):
    """Same path the API uses, so a tester who asks to leave really leaves."""
    h = account["headers"]
    await client.put("/api/v1/files?path=notes.txt", json={"content": "bye"}, headers=h)
    await client.post("/api/v1/deployments", headers=h,
                      json={"name": "site", "kind": "static", "source_dir": "."})

    result = await cli.delete_user(account["username"])
    assert result["workspace_bytes_removed"] > 0

    assert (await client.get("/api/v1/whoami", headers=h)).status_code == 401
    assert (await client.get(f"/u/{account['username']}/")).status_code == 404
    assert account["username"] not in {r["username"] for r in await cli.list_users()}


async def test_deleting_needs_explicit_confirmation():
    """`--yes` exists so the prompt is the default, not the afterthought."""
    parser = cli.build_parser()
    assert parser.parse_args(["user", "delete", "someone"]).yes is False
    assert parser.parse_args(["user", "delete", "someone", "--yes"]).yes is True


async def test_an_unknown_plan_is_refused(client, account):
    with pytest.raises(SystemExit):
        await cli.set_plan(account["username"], "enterprise")


async def test_status_summarises_the_instance(client, account):
    await cli.create_invites(count=2)
    data = await cli.status()

    assert data["revision"] is not None
    assert data["users"]["total"] >= 1
    assert data["unused_invites"] >= 2
    assert data["sandbox_pool"]["budget_mb"] == get_settings().exec_memory_budget_mb
    assert "invite_required" in data["registration"]


async def test_the_parser_covers_every_documented_command():
    """A command in the docstring that argparse rejects is a broken runbook."""
    parser = cli.build_parser()
    for argv in (
        ["status"],
        ["invite", "create", "--count", "5", "--note", "x", "--expires-days", "7"],
        ["invite", "list", "--all"],
        ["invite", "revoke", "ABCD-EFGH-JKLM"],
        ["user", "list"],
        ["user", "suspend", "someone", "--reason", "why"],
        ["user", "restore", "someone"],
        ["user", "password", "someone"],
        ["user", "plan", "someone", "pro"],
    ):
        assert parser.parse_args(argv) is not None


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
def _by_name(checks) -> dict:
    return {c.name: c for c in checks}


async def test_the_default_secret_key_is_critical(client, monkeypatch):
    """It signs session cookies; shipping the default forges every session."""
    monkeypatch.setattr(get_settings(), "secret_key", "dev-only-insecure-key")
    checks = _by_name(await cli.preflight())
    assert checks["secret key"].ok is False
    assert checks["secret key"].severity == cli.CRITICAL


async def test_a_real_secret_key_passes(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "a" * 48)
    assert _by_name(await cli.preflight())["secret key"].ok is True


async def test_the_unsafe_driver_is_critical(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "sandbox_driver", "local_unsafe")
    checks = _by_name(await cli.preflight())
    assert checks["sandbox driver"].ok is False
    assert checks["sandbox driver"].severity == cli.CRITICAL


async def test_the_docker_driver_warns_about_holding_the_socket(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "sandbox_driver", "docker")
    checks = _by_name(await cli.preflight())
    assert checks["control plane privilege"].ok is False


async def test_an_unreachable_broker_is_critical(client, monkeypatch):
    """Otherwise 'safe to expose' would be reported by an instance that cannot run code."""
    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_driver", "remote")
    monkeypatch.setattr(settings, "sandbox_broker_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "sandbox_broker_token", "x" * 32)

    from app.sandbox import get_driver

    get_driver.cache_clear()
    try:
        checks = _by_name(await cli.preflight())
    finally:
        get_driver.cache_clear()

    assert checks["broker reachable"].ok is False
    assert checks["broker reachable"].severity == cli.CRITICAL


async def test_a_short_broker_token_is_critical(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_driver", "remote")
    monkeypatch.setattr(settings, "sandbox_broker_token", "short")
    checks = _by_name(await cli.preflight())
    assert checks["broker token"].ok is False


async def test_egress_and_registration_are_reported(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "sandbox_allow_egress", True)
    monkeypatch.setattr(get_settings(), "invite_required", True)
    checks = _by_name(await cli.preflight())
    assert checks["sandbox egress"].ok is False
    assert checks["registration"].detail == "invite only"


async def test_every_failing_check_says_how_to_fix_it(client, monkeypatch):
    """A checklist that reports a problem without a remedy is just an alarm."""
    monkeypatch.setattr(get_settings(), "secret_key", "dev-only-insecure-key")
    monkeypatch.setattr(get_settings(), "sandbox_allow_egress", True)
    for check in await cli.preflight():
        if not check.ok and check.severity != cli.INFO:
            assert check.fix, f"{check.name} fails without saying what to do"


async def test_suspending_requires_a_reason():
    """A suspension nobody can explain later is one nobody can undo fairly."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["user", "suspend", "someone"])
