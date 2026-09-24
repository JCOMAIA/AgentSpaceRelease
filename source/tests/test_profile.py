"""The page a stranger lands on, and the origin it runs in.

Both properties here exist for the same moment: someone forwards a link in a
chat and a person who has never heard of this site clicks it. The page has to
make sense to them, and it must not be able to act as them.
"""

from __future__ import annotations

import json

import pytest

PAGE = "<!doctype html><meta charset='utf-8'><title>Meu Meme</title><h1>ha</h1>"


async def publish(client, account, path: str, content: str) -> None:
    res = await client.put(
        f"/api/v1/files?path={path}", json={"content": content}, headers=account["headers"]
    )
    assert res.status_code == 200, res.text


# --------------------------------------------------------------------------
# The address people share
# --------------------------------------------------------------------------
async def test_the_handle_url_serves_the_space(client, account):
    await publish(client, account, "public/index.html", PAGE)
    res = await client.get(f"/@{account['username']}/")
    assert res.status_code == 200
    assert "ha" in res.text


async def test_the_old_url_keeps_working_forever(client, account):
    """Links already handed out cannot break. There is no version of this
    product where someone's published URL stops resolving."""
    await publish(client, account, "public/index.html", PAGE)
    assert (await client.get(f"/u/{account['username']}/")).status_code == 200


async def test_the_handle_is_what_the_agent_is_told_to_share(client, account):
    data = (await client.get("/api/v1/whoami", headers=account["headers"])).json()["data"]
    assert f"/@{account['username']}" in json.dumps(data), json.dumps(data)[:400]


async def test_a_page_inside_the_space_is_reachable_by_handle(client, account):
    await publish(client, account, "public/meme.html", PAGE)
    res = await client.get(f"/@{account['username']}/meme.html")
    assert res.status_code == 200


@pytest.mark.parametrize("route", ["/api/v1/hello", "/llms.txt", "/health", "/docs"])
async def test_platform_routes_are_untouched_by_the_handle_namespace(client, route):
    """The whole point of the @: no username can ever shadow a platform path."""
    assert (await client.get(route)).status_code == 200


# --------------------------------------------------------------------------
# What the stranger sees
# --------------------------------------------------------------------------
async def test_the_space_root_is_a_profile_not_a_directory_listing(client, account):
    await publish(client, account, "public/meme.html", PAGE)
    await publish(client, account, "public/tool.html", "<title>Uma Ferramenta</title>")

    body = (await client.get(f"/@{account['username']}/")).text
    assert "Index of" not in body, "still serving a file listing"
    assert f"@{account['username']}" in body
    # Pages are listed by their title, not their filename.
    assert "Meu Meme" in body and "Uma Ferramenta" in body


async def test_the_profile_explains_what_this_site_is(client, account):
    """The visitor has never heard of this place; that is where trust comes from."""
    await publish(client, account, "public/meme.html", PAGE)
    body = (await client.get(f"/@{account['username']}/")).text
    assert "AgentSpace" in body
    assert "live" in body.lower()


async def test_an_index_html_replaces_the_profile(client, account):
    await publish(client, account, "public/meme.html", PAGE)
    assert "Meu Meme" in (await client.get(f"/@{account['username']}/")).text

    await publish(client, account, "public/index.html", "<h1>minha capa</h1>")
    body = (await client.get(f"/@{account['username']}/")).text
    assert "minha capa" in body
    assert "Meu Meme" not in body


async def test_the_profile_reads_name_and_bio_from_profile_json(client, account):
    await publish(client, account, "public/meme.html", PAGE)
    await publish(client, account, "profile.json", json.dumps(
        {"name": "João Maia", "bio": "faço memes com IA", "links": [
            {"label": "discord", "url": "https://discord.gg/example"}]}))

    body = (await client.get(f"/@{account['username']}/")).text
    assert "João Maia" in body
    assert "faço memes com IA" in body
    assert "https://discord.gg/example" in body


async def test_a_broken_profile_json_degrades_instead_of_500ing(client, account):
    """It is written by an agent improvising, so half of them will be wrong."""
    await publish(client, account, "profile.json", "{not json at all")
    res = await client.get(f"/@{account['username']}/")
    assert res.status_code == 200
    assert account["username"] in res.text


async def test_profile_json_is_not_itself_published(client, account):
    await publish(client, account, "profile.json", '{"name": "x"}')
    assert (await client.get(f"/@{account['username']}/profile.json")).status_code == 404


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,<script>x</script>"])
async def test_profile_links_cannot_smuggle_a_script(client, account, bad):
    await publish(client, account, "profile.json", json.dumps(
        {"name": "x", "links": [{"label": "click", "url": bad}]}))
    body = (await client.get(f"/@{account['username']}/")).text
    assert "javascript:" not in body
    assert "data:text/html" not in body


async def test_a_bio_cannot_inject_html(client, account):
    await publish(client, account, "profile.json", json.dumps(
        {"name": "x", "bio": "<script>alert(1)</script>"}))
    body = (await client.get(f"/@{account['username']}/")).text
    assert "<script>alert(1)</script>" not in body


async def test_an_avatar_cannot_point_outside_the_space(client, account):
    await publish(client, account, "profile.json", json.dumps(
        {"name": "x", "avatar": "../../../etc/passwd"}))
    body = (await client.get(f"/@{account['username']}/")).text
    assert "etc/passwd" not in body


async def test_hidden_files_never_appear_on_the_profile(client, account):
    await publish(client, account, "public/.env", "OPENAI_API_KEY=sk-real")
    await publish(client, account, "public/ok.html", PAGE)
    body = (await client.get(f"/@{account['username']}/")).text
    assert ".env" not in body
    assert "sk-real" not in body


# --------------------------------------------------------------------------
# The origin published pages run in
# --------------------------------------------------------------------------
async def test_published_pages_cannot_act_as_whoever_opens_them(client, account):
    """Without this, one shared link is account takeover.

    Published files are served from the same origin as the dashboard, so a
    `<script>` in someone's page can POST to /api/v1/account/keys and the browser
    attaches the visitor's session cookie by itself — httponly does not help,
    because the script never touches the cookie. A sandbox CSP with no
    `allow-same-origin` puts the page in an opaque origin, where that request
    carries no credentials at all.
    """
    await publish(client, account, "public/index.html", PAGE)
    csp = (await client.get(f"/@{account['username']}/")).headers.get(
        "content-security-policy", ""
    )
    assert "sandbox" in csp, "user content is running on the platform origin"
    assert "allow-same-origin" not in csp, "the sandbox is not actually isolating anything"


@pytest.mark.parametrize("path,content", [
    ("public/page.html", PAGE),
    ("public/app.js", "console.log(1)"),
    ("public/data.json", "{}"),
])
async def test_every_published_file_is_sandboxed_not_just_html(client, account, path, content):
    await publish(client, account, path, content)
    served = f"/@{account['username']}/{path.removeprefix('public/')}"
    csp = (await client.get(served)).headers.get("content-security-policy", "")
    assert "sandbox" in csp and "allow-same-origin" not in csp


async def test_the_platform_pages_are_not_sandboxed(client, account):
    """The profile and the dashboard are ours; sandboxing them would be cargo cult."""
    await publish(client, account, "public/meme.html", PAGE)
    for route in [f"/@{account['username']}/", "/"]:
        csp = (await client.get(route)).headers.get("content-security-policy", "")
        assert "sandbox" not in csp, route


# --------------------------------------------------------------------------
# Freshness, which the whole iterate-with-a-chatbot loop depends on
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path,content", [
    ("public/index.html", PAGE),
    ("public/style.css", "body{margin:0}"),
    ("public/app.js", "console.log(1)"),
])
async def test_published_files_must_be_revalidated_not_reused(client, account, path, content):
    """With no policy, caches invent one and serve a stored copy without asking.

    A hosted chatbot told to re-read a page it had seen before reported the
    space as empty hours after it had been filled, because its fetcher still
    held the copy from when it was. Every workflow here ends in "look at it
    again", so nothing may be served from a cache without checking first.
    """
    await publish(client, account, path, content)
    served = f"/@{account['username']}/{path.removeprefix('public/')}"
    cache = (await client.get(served)).headers.get("cache-control", "")
    assert "no-cache" in cache, f"{path} can be served stale"


async def test_revalidating_an_unchanged_page_costs_no_body(client, account):
    """`no-cache` only stays cheap if the check is actually answered.

    Starlette's FileResponse sets an ETag and then ignores `If-None-Match`, so
    every revalidation was replying 200 with the whole file — the header said
    "just ask me" and the server resent everything anyway.
    """
    await publish(client, account, "public/style.css", "body{margin:0}")
    served = f"/@{account['username']}/style.css"

    first = await client.get(served)
    assert first.status_code == 200
    etag = first.headers["etag"]

    again = await client.get(served, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content


async def test_a_changed_page_is_sent_again_despite_the_old_validator(client, account):
    await publish(client, account, "public/style.css", "body{margin:0}")
    served = f"/@{account['username']}/style.css"
    stale = (await client.get(served)).headers["etag"]

    await publish(client, account, "public/style.css", "body{margin:99px}")
    fresh = await client.get(served, headers={"If-None-Match": stale})
    assert fresh.status_code == 200
    assert "99px" in fresh.text


async def test_the_profile_is_revalidated_too(client, account):
    await publish(client, account, "public/a.html", PAGE)
    cache = (await client.get(f"/@{account['username']}/")).headers.get("cache-control", "")
    assert "no-cache" in cache


async def test_an_edited_page_is_not_served_from_a_stored_copy(client, account):
    """The behavioural half: same URL, new bytes, no revalidation needed to see it."""
    await publish(client, account, "public/index.html", "<title>v1</title><h1>um</h1>")
    first = await client.get(f"/@{account['username']}/")
    assert "um" in first.text

    await publish(client, account, "public/index.html", "<title>v2</title><h1>dois</h1>")
    second = await client.get(f"/@{account['username']}/")
    assert "dois" in second.text
    assert "um" not in second.text


async def test_scripts_still_run_in_published_pages(client, account):
    """The sandbox must not cost the thing the space is for."""
    await publish(client, account, "public/index.html", PAGE)
    csp = (await client.get(f"/@{account['username']}/")).headers["content-security-policy"]
    assert "allow-scripts" in csp
    assert "allow-forms" in csp
