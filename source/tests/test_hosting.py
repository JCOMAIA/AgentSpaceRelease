"""Account lifecycle and public hosting."""

from __future__ import annotations

import pytest


async def test_registration_issues_a_working_key_immediately(client):
    res = await client.post(
        "/api/v1/account/register",
        json={"username": "firstrun", "email": "firstrun@example.com",
              "password": "long-enough-password"},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["api_key"].startswith("ask_")

    whoami = await client.get(
        "/api/v1/whoami", headers={"Authorization": f"Bearer {data['api_key']}"}
    )
    assert whoami.json()["data"]["username"] == "firstrun"


@pytest.fixture
def count_commits(monkeypatch):
    """Count session commits across a request."""
    from sqlalchemy.ext.asyncio import AsyncSession

    calls = {"n": 0}
    original = AsyncSession.commit

    async def counting(self):
        calls["n"] += 1
        return await original(self)

    monkeypatch.setattr(AsyncSession, "commit", counting)
    return calls


async def test_a_freshly_issued_key_is_durable_before_it_is_handed_over(client, count_commits):
    """Otherwise a caller fast enough to use the key beats the write to disk.

    The register endpoint used to `flush()` and leave the commit to dependency
    teardown, which runs after the response is already on its way. An agent —
    the normal caller here — would register and immediately get
    `invalid_api_key` for the key it had just been given. It was near-certain on
    a brand-new database, where the first commit also has to build the WAL, so
    the very first account on a fresh deployment was the one that hit it.

    The symptom needs a real socket to reproduce: the in-process transport runs
    teardown before returning, so the race cannot exist here. What is pinned
    instead is the mechanism — register must commit itself, which shows up as a
    commit of its own on top of the one teardown always does.
    """
    res = await client.post(
        "/api/v1/account/register",
        json={"username": "corrida", "email": "corrida@example.com",
              "password": "long-enough-password"},
    )
    assert res.status_code == 200
    assert count_commits["n"] >= 2, "register left the commit to dependency teardown"


async def test_a_key_minted_through_the_api_is_durable_too(client, account, count_commits):
    res = await client.post(
        "/api/v1/account/keys", json={"name": "second"}, headers=account["headers"]
    )
    assert res.status_code == 200
    assert count_commits["n"] >= 2, "key creation left the commit to dependency teardown"


@pytest.mark.parametrize("username", ["api", "mcp", "a2a", "admin", "dashboard", "www"])
async def test_reserved_usernames_are_refused(client, username):
    res = await client.post(
        "/api/v1/account/register",
        json={"username": username, "email": f"{username}@example.com",
              "password": "long-enough-password"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "reserved_username"


@pytest.mark.parametrize("username", ["-bad", "bad-", "a", "has space", "wow!", "under_score"])
async def test_malformed_usernames_are_refused(client, username):
    res = await client.post(
        "/api/v1/account/register",
        json={"username": username, "email": "x@example.com", "password": "long-enough-password"},
    )
    assert res.status_code in (400, 422)


async def test_username_case_is_normalised_not_rejected(client):
    """Usernames become URLs, which are case-insensitive — so fold rather than refuse."""
    res = await client.post(
        "/api/v1/account/register",
        json={"username": "MixedCase", "email": "mixed@example.com",
              "password": "long-enough-password"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["username"] == "mixedcase"


async def test_duplicate_username_is_rejected(client, account):
    res = await client.post(
        "/api/v1/account/register",
        json={"username": account["username"], "email": "other@example.com",
              "password": "long-enough-password"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "username_taken"


async def test_login_failure_does_not_reveal_whether_the_email_exists(client, account):
    known = await client.post(
        "/api/v1/account/login",
        json={"email": f"{account['username']}@example.com", "password": "wrong-password"},
    )
    unknown = await client.post(
        "/api/v1/account/login",
        json={"email": "nobody@example.com", "password": "wrong-password"},
    )
    assert known.status_code == unknown.status_code == 401
    assert known.json()["error"] == unknown.json()["error"]


async def test_revoked_key_stops_working(client, account):
    h = account["headers"]
    listing = await client.get("/api/v1/account/keys", headers=h)
    key_id = listing.json()["data"]["keys"][0]["id"]

    await client.delete(f"/api/v1/account/keys/{key_id}", headers=h)

    res = await client.get("/api/v1/whoami", headers=h)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_api_key"


async def test_free_plan_cannot_attach_a_custom_domain(client, account):
    res = await client.put(
        "/api/v1/account/domain", json={"domain": "example.com"}, headers=account["headers"]
    )
    assert res.status_code == 402
    assert res.json()["error"]["code"] == "plan_forbids_custom_domain"


# --------------------------------------------------------------------------
# Public hosting
# --------------------------------------------------------------------------
async def test_static_site_is_served_at_the_path_url(client, account):
    h = account["headers"]
    await client.put(
        "/api/v1/files?path=www/index.html",
        json={"content": "<h1>hello from an agent</h1>"}, headers=h,
    )
    await client.put(
        "/api/v1/files?path=www/style.css", json={"content": "body{color:red}"}, headers=h
    )
    res = await client.post(
        "/api/v1/deployments",
        json={"name": "site", "kind": "static", "source_dir": "www"}, headers=h,
    )
    assert res.status_code == 200, res.text

    page = await client.get(f"/u/{account['username']}/site/")
    assert page.status_code == 200
    assert "hello from an agent" in page.text

    css = await client.get(f"/u/{account['username']}/site/style.css")
    assert css.status_code == 200
    assert "color:red" in css.text


async def test_single_deployment_becomes_the_space_homepage(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>home</p>"}, headers=h)
    await client.post(
        "/api/v1/deployments", json={"name": "only", "kind": "static", "source_dir": "."},
        headers=h,
    )
    res = await client.get(f"/u/{account['username']}/")
    assert res.status_code == 200
    assert "home" in res.text


async def test_subdomain_resolves_to_the_same_space(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>via host</p>"},
                     headers=h)
    await client.post(
        "/api/v1/deployments", json={"name": "site", "kind": "static", "source_dir": "."},
        headers=h,
    )
    res = await client.get("/", headers={"Host": f"{account['username']}.agentspace.test"})
    assert res.status_code == 200
    assert "via host" in res.text


async def test_platform_paths_are_not_shadowed_by_a_subdomain(client, account):
    res = await client.get(
        "/api/v1/whoami",
        headers={**account["headers"], "Host": f"{account['username']}.agentspace.test"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["username"] == account["username"]


async def test_unknown_space_gets_a_readable_404(client):
    res = await client.get("/u/nobody-here/")
    assert res.status_code == 404
    assert "no space called" in res.text.lower()


async def test_an_empty_space_is_an_empty_profile_not_a_404(client, account):
    """The account exists, so the profile exists — it just has nothing on it yet.

    A 404 would tell a visitor the person is not here at all, and would tell the
    owner their brand-new space is broken.
    """
    res = await client.get(f"/@{account['username']}/")
    assert res.status_code == 200
    assert account["username"] in res.text
    assert "Nothing here yet" in res.text
    assert "public/" in res.text


async def test_a_space_that_does_not_exist_still_404s(client):
    res = await client.get("/@nobody-at-all/")
    assert res.status_code == 404
    assert "no space called" in res.text.lower()


# --------------------------------------------------------------------------
# public/ is the site, with no deploy call
# --------------------------------------------------------------------------
async def test_writing_to_public_puts_it_on_the_web_with_no_second_call(client, account):
    """The property the whole product rests on: a link that works right away.

    If publishing needed a second call, an agent that wrote the page and stopped
    would hand over a URL that 404s — and the person receiving it would decide
    the thing is broken.
    """
    await client.put(
        "/api/v1/files?path=public/index.html",
        json={"content": "<!doctype html><title>Meme</title><h1>it is live</h1>"},
        headers=account["headers"],
    )
    res = await client.get(f"/u/{account['username']}/")
    assert res.status_code == 200
    assert "it is live" in res.text


async def test_public_pages_keep_their_own_addresses(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=public/tool.html",
                     json={"content": "<h1>a tool</h1>"}, headers=h)
    res = await client.get(f"/u/{account['username']}/tool.html")
    assert res.status_code == 200
    assert "a tool" in res.text


async def test_files_outside_public_are_never_served(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=notes.md", json={"content": "SECRET"}, headers=h)
    await client.put("/api/v1/files?path=public/index.html",
                     json={"content": "<h1>hi</h1>"}, headers=h)

    leak = await client.get(f"/u/{account['username']}/notes.md")
    assert leak.status_code == 404
    assert "SECRET" not in leak.text


async def test_deleting_from_public_takes_it_off_the_web(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=public/index.html",
                     json={"content": "<h1>front</h1>"}, headers=h)
    await client.put("/api/v1/files?path=public/gone.html",
                     json={"content": "<h1>bye</h1>"}, headers=h)
    assert (await client.get(f"/u/{account['username']}/gone.html")).status_code == 200

    await client.delete("/api/v1/files?path=public/gone.html", headers=h)
    res = await client.get(f"/u/{account['username']}/gone.html")
    assert res.status_code == 404, "a deleted page must not keep answering 200 behind the shell"
    assert "front" not in res.text


async def test_hidden_files_in_public_are_never_served(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=public/.env",
                     json={"content": "OPENAI_API_KEY=sk-real"}, headers=h)
    res = await client.get(f"/u/{account['username']}/.env")
    assert res.status_code == 404
    assert "sk-real" not in res.text


async def test_public_assets_are_typed_by_us_not_by_the_host(client, account):
    """A page that cannot load its own script is not a page."""
    h = account["headers"]
    for path, content in [("public/index.html", "<script src='app.js'></script>"),
                          ("public/app.js", "console.log(1)"),
                          ("public/style.css", "body{margin:0}")]:
        await client.put(f"/api/v1/files?path={path}", json={"content": content}, headers=h)

    base = f"/u/{account['username']}"
    js = await client.get(f"{base}/app.js")
    assert js.headers["content-type"].startswith("text/javascript"), js.headers["content-type"]
    assert js.headers["x-content-type-options"] == "nosniff"
    assert (await client.get(f"{base}/style.css")).headers["content-type"].startswith("text/css")


async def test_a_client_routed_app_still_gets_its_shell(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=public/index.html",
                     json={"content": "<title>SPA</title>"}, headers=h)
    res = await client.get(f"/u/{account['username']}/some/route")
    assert res.status_code == 200
    assert "SPA" in res.text


async def test_an_explicit_deployment_still_wins_over_public(client, account):
    """Nobody's existing site breaks because public/ became implicit."""
    h = account["headers"]
    await client.put("/api/v1/files?path=public/index.html",
                     json={"content": "<h1>implicit</h1>"}, headers=h)
    await client.put("/api/v1/files?path=www/index.html",
                     json={"content": "<h1>explicit</h1>"}, headers=h)
    await client.post(
        "/api/v1/deployments", json={"name": "site", "kind": "static", "source_dir": "www"},
        headers=h,
    )
    assert "explicit" in (await client.get(f"/u/{account['username']}/site/")).text


async def test_published_site_cannot_reach_outside_its_directory(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=private.txt", json={"content": "SECRET"}, headers=h)
    await client.put("/api/v1/files?path=www/index.html", json={"content": "<p>ok</p>"},
                     headers=h)
    await client.post(
        "/api/v1/deployments", json={"name": "site", "kind": "static", "source_dir": "www"},
        headers=h,
    )

    leak = await client.get(f"/u/{account['username']}/site/../private.txt")
    assert "SECRET" not in leak.text


async def test_static_deployment_updates_without_redeploying(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>v1</p>"}, headers=h)
    await client.post(
        "/api/v1/deployments", json={"name": "site", "kind": "static", "source_dir": "."},
        headers=h,
    )
    assert "v1" in (await client.get(f"/u/{account['username']}/site/")).text

    await client.put("/api/v1/files?path=index.html", json={"content": "<p>v2</p>"}, headers=h)
    assert "v2" in (await client.get(f"/u/{account['username']}/site/")).text


async def test_deleting_a_deployment_takes_it_offline_but_keeps_the_files(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>bye</p>"}, headers=h)
    await client.post(
        "/api/v1/deployments", json={"name": "site", "kind": "static", "source_dir": "."},
        headers=h,
    )
    await client.delete("/api/v1/deployments/site", headers=h)

    assert (await client.get(f"/u/{account['username']}/site/")).status_code == 404
    assert (await client.get("/api/v1/files/content?path=index.html",
                             headers=h)).status_code == 200


async def test_logs_on_a_static_site_explain_why_there_are_none(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>x</p>"}, headers=h)
    await client.post(
        "/api/v1/deployments", json={"name": "site", "kind": "static", "source_dir": "."},
        headers=h,
    )
    res = await client.get("/api/v1/deployments/site/logs", headers=h)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "no_logs"
