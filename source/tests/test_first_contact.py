"""What the space says about itself on the very first request.

`/api/v1/hello` is the most-read document here: an agent with no memory of any
documentation starts there and does whatever it says. Every sentence in it is
load-bearing, and a sentence that is false costs a whole session.

These tests exist because a live trial with three hosted chatbots found the
document teaching the wrong product — it promised a sandbox on a box that runs
nothing, never mentioned `public/`, and advertised a subdomain URL that did not
resolve. One model faithfully followed it and reported the old deploy-first
flow; another produced a dead link in exactly the shape the document suggested.
"""

from __future__ import annotations

import pytest

from app.config import get_settings


@pytest.fixture
def publish_only(monkeypatch):
    from app import sandbox

    monkeypatch.setattr(get_settings(), "sandbox_driver", "none")
    sandbox.get_driver.cache_clear()
    yield
    sandbox.get_driver.cache_clear()


async def hello(client) -> dict:
    res = await client.get("/api/v1/hello")
    assert res.status_code == 200
    return res.json()["data"]


# --------------------------------------------------------------------------
# The loop has to be in the briefing
# --------------------------------------------------------------------------
async def test_first_contact_leads_with_the_publish_loop(client):
    """An agent that has to infer `public/` will not infer it."""
    data = await hello(client)
    assert "how_to_publish" in data
    steps = " ".join(data["how_to_publish"])
    assert "public/index.html" in steps
    assert "already live" in steps

    model = " ".join(data["mental_model"])
    assert "public/" in model
    assert "no deploy step" in model


async def test_first_contact_says_public_is_the_website(client):
    data = await hello(client)
    assert "public/" in data["conventions"]["publishing"]
    assert data["your_space"]["the_published_folder"] == "public/"


# --------------------------------------------------------------------------
# It must not promise what this instance cannot do
# --------------------------------------------------------------------------
async def test_a_publish_only_space_does_not_promise_a_sandbox(client, publish_only):
    data = await hello(client)
    model = " ".join(data["mental_model"])
    assert "sandbox" not in model.lower(), model
    assert "run code" not in model.lower(), model


async def test_a_publish_only_space_says_so_outright(client, publish_only):
    data = await hello(client)
    assert "does not run code" in data["important"]
    assert "visitor's browser" in data["important"]


async def test_a_sandboxed_space_still_mentions_its_sandbox(client):
    """The default deployment must keep advertising what it really has."""
    model = " ".join((await hello(client))["mental_model"])
    assert "sandbox" in model.lower()


# --------------------------------------------------------------------------
# Never hand out an address that does not resolve
# --------------------------------------------------------------------------
async def test_subdomain_urls_are_not_advertised_by_default(client, account):
    """Wildcard DNS is not something the app can detect, so it must not assume.

    Behind a tunnel `<name>.<host>` resolves nowhere. An agent given that shape
    passes it to a person, and the publish that worked becomes a broken link.
    """
    data = await hello(client)
    assert "public_subdomain_url" not in data["your_space"]

    whoami = (await client.get("/api/v1/whoami", headers=account["headers"])).json()["data"]
    assert "subdomain" not in whoami["urls"]
    assert whoami["urls"]["path"].startswith("http")


async def test_subdomain_urls_appear_when_the_operator_enables_them(
    client, account, monkeypatch
):
    monkeypatch.setattr(get_settings(), "subdomain_urls", True)
    data = await hello(client)
    assert "public_subdomain_url" in data["your_space"]

    whoami = (await client.get("/api/v1/whoami", headers=account["headers"])).json()["data"]
    assert whoami["urls"]["subdomain"].startswith("https://")


# --------------------------------------------------------------------------
# /llms.txt is read too, and separately
# --------------------------------------------------------------------------
async def manual(client) -> str:
    res = await client.get("/llms.txt")
    assert res.status_code == 200
    return res.text


async def test_the_manual_leads_with_the_loop(client):
    """A manual that buries the loop gets summarised without it.

    A hosted chatbot fetched `/llms.txt` alongside `/api/v1/hello` and reported
    the deploy-first flow, because that is what the Publishing section still
    said. It read faithfully; the document was wrong. Both documents have to
    teach the same product.
    """
    text = await manual(client)
    loop = text.split("## Authentication")[0]
    assert "public/index.html" in loop, "the loop is not above the fold"
    assert "no publish call" in loop


async def test_the_manual_does_not_promise_a_sandbox_that_is_off(client, publish_only):
    text = (await manual(client)).lower()
    assert "code sandbox" not in text
    assert "give a command and a port" not in text, "still offering to run a process"


async def test_the_manual_keeps_deployments_where_they_work(client):
    text = await manual(client)
    assert "give a command and a port" in text


async def test_the_manual_does_not_advertise_a_dead_subdomain(client):
    text = await manual(client)
    assert f"<username>.{get_settings().base_domain}" not in text


async def test_the_manual_advertises_subdomains_when_they_work(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "subdomain_urls", True)
    text = await manual(client)
    assert f"<username>.{get_settings().base_domain}" in text


async def test_the_handle_url_is_always_offered(client, account):
    """Whatever else is on or off, there is always one address that works."""
    data = await hello(client)
    assert data["your_space"]["public_site_url"].startswith("http")
    assert "/@" in data["your_space"]["public_site_url"]
