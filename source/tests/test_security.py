"""Regressions for holes found by auditing the running server.

Each test here corresponds to something that was confirmed exploitable, not to
something that looked risky in review.
"""

from __future__ import annotations

import pytest

from app import ratelimit


async def _publish(client, headers, source_dir="."):
    await client.put(
        f"/api/v1/files?path={source_dir}/index.html".replace("./", ""),
        json={"content": "<h1>public</h1>"},
        headers=headers,
    )
    res = await client.post(
        "/api/v1/deployments",
        json={"name": "site", "kind": "static", "source_dir": source_dir},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------
# A + B: publishing a directory must not publish its secrets
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "secret_path",
    [".env", ".git/config", ".ssh/id_rsa", "sub/.env", ".agentspace/uploads/x.txt"],
)
async def test_hidden_files_are_never_served(client, account, secret_path):
    h = account["headers"]
    await client.put(
        f"/api/v1/files?path={secret_path}", json={"content": "SUPER_SECRET_VALUE"}, headers=h
    )
    await _publish(client, h)

    res = await client.get(f"/u/{account['username']}/{secret_path}")
    assert res.status_code == 404, f"{secret_path} was served"
    assert "SUPER_SECRET_VALUE" not in res.text


async def test_hidden_files_are_refused_before_checking_existence(client, account):
    """The answer must not differ for files that exist and files that do not."""
    h = account["headers"]
    await _publish(client, h)

    present = await client.get(f"/u/{account['username']}/.env")
    absent = await client.get(f"/u/{account['username']}/.does-not-exist")
    assert present.status_code == absent.status_code == 404
    assert "Hidden files are never published" in present.text


async def test_directory_listing_hides_hidden_entries(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=pub/.env", json={"content": "k=v"}, headers=h)
    await client.put("/api/v1/files?path=pub/readme.txt", json={"content": "hi"}, headers=h)
    res = await client.post(
        "/api/v1/deployments",
        json={"name": "listing", "kind": "static", "source_dir": "pub"},
        headers=h,
    )
    assert res.status_code == 200

    page = await client.get(f"/u/{account['username']}/listing/")
    assert "readme.txt" in page.text
    assert ".env" not in page.text


async def test_publishing_warns_about_credential_shaped_files(client, account):
    """Hidden files are blocked, but a normally-named secret is still public."""
    h = account["headers"]
    await client.put(
        "/api/v1/files?path=www/credentials.json", json={"content": "{}"}, headers=h
    )
    await client.put("/api/v1/files?path=www/server.pem", json={"content": "x"}, headers=h)
    await client.put("/api/v1/files?path=www/index.html", json={"content": "<p>ok</p>"},
                     headers=h)

    res = await client.post(
        "/api/v1/deployments",
        json={"name": "warned", "kind": "static", "source_dir": "www"},
        headers=h,
    )
    notes = " ".join(res.json()["guide"]["notes"])
    assert "WILL be served publicly" in notes
    assert "credentials.json" in notes
    assert "server.pem" in notes


async def test_publishing_always_says_the_directory_becomes_public(client, account):
    h = account["headers"]
    res = await _publish(client, h)
    notes = " ".join(res["guide"]["notes"])
    assert "becomes public" in notes


# --------------------------------------------------------------------------
# C + D: unlimited credential guessing and account minting
# --------------------------------------------------------------------------
async def test_login_attempts_are_rate_limited(client, account):
    ratelimit.reset()
    email = f"{account['username']}@example.com"

    statuses = []
    for _ in range(15):
        res = await client.post(
            "/api/v1/account/login", json={"email": email, "password": "wrong-password"}
        )
        statuses.append(res.status_code)

    assert 429 in statuses, "brute force ran unbounded"
    blocked = statuses.index(429)
    assert blocked <= 10, f"took {blocked} attempts before blocking"


async def test_rate_limit_response_says_how_long_to_wait(client):
    ratelimit.reset()
    for _ in range(20):
        res = await client.post(
            "/api/v1/account/login",
            json={"email": "someone@example.com", "password": "nope"},
        )
        if res.status_code == 429:
            break
    error = res.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"]["retry_after_seconds"] > 0
    assert error["fix"]


async def test_registration_is_rate_limited(client):
    ratelimit.reset()
    statuses = []
    for i in range(10):
        res = await client.post(
            "/api/v1/account/register",
            json={
                "username": f"flood{i}",
                "email": f"flood{i}@example.com",
                "password": "long-enough-password",
            },
        )
        statuses.append(res.status_code)
    assert 429 in statuses, "accounts could be minted without limit"


async def test_a_correct_password_still_works_below_the_limit(client):
    ratelimit.reset()
    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "goodlogin", "email": "goodlogin@example.com",
              "password": "long-enough-password"},
    )
    assert reg.status_code == 200
    for _ in range(3):
        await client.post(
            "/api/v1/account/login",
            json={"email": "goodlogin@example.com", "password": "wrong"},
        )
    res = await client.post(
        "/api/v1/account/login",
        json={"email": "goodlogin@example.com", "password": "long-enough-password"},
    )
    assert res.status_code == 200


# --------------------------------------------------------------------------
# E: the user must be able to erase themselves
# --------------------------------------------------------------------------
async def test_account_deletion_removes_data_and_access(client):
    ratelimit.reset()
    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "leaving", "email": "leaving@example.com",
              "password": "long-enough-password"},
    )
    headers = {"Authorization": f"Bearer {reg.json()['data']['api_key']}"}
    await client.put("/api/v1/files?path=notes.txt", json={"content": "mine"}, headers=headers)

    res = await client.request(
        "DELETE",
        "/api/v1/account",
        json={"password": "long-enough-password", "confirm": "leaving"},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["deleted"] is True

    # The key must stop working immediately.
    assert (await client.get("/api/v1/whoami", headers=headers)).status_code == 401
    # And the space must be gone, not merely hidden.
    assert (await client.get("/u/leaving/")).status_code == 404


async def test_deletion_requires_the_password(client, account):
    res = await client.request(
        "DELETE",
        "/api/v1/account",
        json={"password": "not-the-password", "confirm": account["username"]},
        headers=account["headers"],
    )
    assert res.status_code == 401
    assert (await client.get("/api/v1/whoami", headers=account["headers"])).status_code == 200


async def test_deletion_requires_typing_the_username(client, account):
    res = await client.request(
        "DELETE",
        "/api/v1/account",
        json={"password": "a-long-enough-password", "confirm": "wrong"},
        headers=account["headers"],
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "confirmation_mismatch"
