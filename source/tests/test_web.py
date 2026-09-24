"""The human-facing pages.

These exist because the rest of the suite only ever exercised JSON, which let a
template-rendering break reach a running server unnoticed.
"""

from __future__ import annotations

import pytest

from app.deps import SESSION_COOKIE
from app.security import sign_session


@pytest.mark.parametrize("path", ["/", "/register", "/login"])
async def test_public_pages_render(client, path):
    res = await client.get(path)
    assert res.status_code == 200, res.text[:400]
    assert res.headers["content-type"].startswith("text/html")
    assert "<html" in res.text.lower()


async def test_landing_lists_the_capabilities_and_all_three_doors(client):
    res = await client.get("/")
    assert "/api/v1" in res.text
    assert "/mcp" in res.text
    assert "agent-card.json" in res.text
    assert "Publish a static website" in res.text or "Publish a static site" in res.text


async def test_dashboard_requires_login(client):
    res = await client.get("/dashboard", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login"


async def test_dashboard_shows_usage_and_endpoints(client):
    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "dashuser", "email": "dash@example.com",
              "password": "long-enough-password"},
    )
    assert reg.status_code == 200
    assert client.cookies.get(SESSION_COOKIE), "registration should establish a session"

    res = await client.get("/dashboard")
    assert res.status_code == 200, res.text[:400]
    assert "dashuser" in res.text
    assert "/mcp" in res.text
    assert "agent-card.json" in res.text


async def test_a_forged_session_cookie_is_rejected(client):
    client.cookies.set(SESSION_COOKIE, "bm90LWEtcmVhbC10b2tlbg.ZmFrZS1zaWduYXR1cmU")
    res = await client.get("/dashboard", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login"


async def test_a_session_for_a_deleted_user_is_rejected(client):
    client.cookies.set(SESSION_COOKIE, sign_session("ffffffffffffffffffffffffffffffff"))
    res = await client.get("/dashboard", follow_redirects=False)
    assert res.status_code == 302


async def test_static_assets_are_served(client):
    for path in ("/static/style.css", "/static/icon.svg"):
        res = await client.get(path)
        assert res.status_code == 200, path


async def test_favicon_points_at_the_real_icon(client):
    res = await client.get("/favicon.ico", follow_redirects=False)
    assert res.status_code == 301
    assert res.headers["location"] == "/static/icon.svg"


async def test_the_plan_card_is_legible_where_nothing_executes(client, monkeypatch):
    """The free tier's headline number must survive being rendered.

    `disk_mb / 1024` printed "0 GB storage" for a 100 MB plan, and the sandbox
    lines rendered as "s per run" and "Sleeps after  min idle" once the
    execution limits stopped being published. All three only appear once a
    Stripe key is set, so nothing caught them until the day of the launch.
    """
    from app import sandbox
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_not_a_real_key")
    monkeypatch.setattr(get_settings(), "sandbox_driver", "none")
    sandbox.get_driver.cache_clear()
    try:
        await client.post(
            "/api/v1/account/register",
            json={"username": "planlook", "email": "plan@example.com",
                  "password": "long-enough-password"},
        )
        res = await client.get("/dashboard")
        assert res.status_code == 200, res.text[:400]

        assert "0 GB storage" not in res.text
        assert "100 MB" in res.text, "the free tier's own size should be on its card"
        # Labels whose number went missing read as these fragments.
        for orphan in ("s per run", "Sleeps after  min", ">0 GB", "CPU sandbox"):
            assert orphan not in res.text, orphan
    finally:
        # A poisoned driver cache would follow this test into every later one.
        sandbox.get_driver.cache_clear()


async def test_pricing_is_public_and_does_not_need_stripe(client):
    """A stranger must be able to find out the price before signing up.

    The table lived only on the dashboard, behind both a login and a configured
    Stripe key, so on a fresh instance the prices were unreachable.
    """
    res = await client.get("/pricing")
    assert res.status_code == 200, res.text[:300]
    assert "€3" in res.text and "€9" in res.text
    assert "100 MB storage" in res.text
    assert "Personal" not in res.text, "the private tier is not for sale"
    # No Stripe key is configured here, so nothing may imply it can be bought.
    assert "Not open yet" in res.text


async def test_pricing_says_nothing_about_a_sandbox_it_does_not_have(client, monkeypatch):
    from app import sandbox
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_driver", "none")
    sandbox.get_driver.cache_clear()
    try:
        res = await client.get("/pricing")
        assert "CPU sandbox" not in res.text
        assert "does not run code" in res.text
    finally:
        sandbox.get_driver.cache_clear()


async def test_personal_mode_has_nothing_to_sell(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "single_user_mode", True)
    res = await client.get("/pricing", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/"


async def test_static_urls_change_when_the_file_does(client, tmp_path, monkeypatch):
    """A stable URL for changing bytes is a stale stylesheet at the edge.

    Cloudflare held style.css at max-age=14400 while the origin served a new
    one, so a template deployed with a new CSS class rendered unstyled for four
    hours. Server-rendered HTML ships instantly; its assets do not, unless the
    URL moves with the content.
    """
    from app import templating

    res = await client.get("/pricing")
    assert 'href="/static/style.css?v=' in res.text, res.text[:600]

    asset = tmp_path / "style.css"
    asset.write_bytes(b"body{color:red}")
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(templating, "_fingerprints", {})
    first = templating.static_url("style.css")

    asset.write_bytes(b"body{color:blue}")
    monkeypatch.setattr(templating, "_fingerprints", {})
    assert templating.static_url("style.css") != first


async def test_a_missing_asset_does_not_take_the_page_down(tmp_path, monkeypatch):
    from app import templating

    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(templating, "_fingerprints", {})
    assert templating.static_url("nope.css") == "/static/nope.css"


def test_every_template_environment_knows_about_assets():
    """base.html is shared by three environments, and a global registered on
    one of them renders as an empty string in the other two -- which is a 404
    for the stylesheet on the profile pages strangers actually land on."""
    from app import hosting, publish_routes, web

    for module in (web, hosting, publish_routes):
        rendered = module.templates.env.globals["static_url"]("style.css")
        assert rendered.startswith("/static/style.css?v="), module.__name__
