"""The shape used when the space is open to people you do not know.

With `SANDBOX_DRIVER=none` there is no sandbox, so there is nothing to escape:
the worst a stranger can do is publish a bad file, which a person can moderate.
That only holds if the space stops advertising execution — a catalogue that
promises `run_code` on a box that cannot run code costs every agent a turn to
find out, and costs the operator a support question.
"""

from __future__ import annotations

import pytest

from app import mcp_server, sandbox
from app.config import get_settings
from app.teaching import capability_table

EXEC_IDS = {"exec.run", "deploy.service", "deploy.logs"}
EXEC_TOOLS = {"run_code", "deploy_service", "deployment_logs"}


@pytest.fixture
def publish_only(monkeypatch):
    """Switch the running app to publish-only for one test."""
    monkeypatch.setattr(get_settings(), "sandbox_driver", "none")
    # The driver is cached for the process, so the switch has to invalidate it
    # or the previous driver keeps answering.
    sandbox.get_driver.cache_clear()
    yield
    sandbox.get_driver.cache_clear()


# --------------------------------------------------------------------------
# What the space says it can do
# --------------------------------------------------------------------------
def test_the_catalogue_stops_promising_execution(publish_only):
    ids = {c["id"] for c in capability_table()}
    assert not (ids & EXEC_IDS), f"still advertising {ids & EXEC_IDS}"
    # And keeps everything that still works.
    assert {"files.write", "files.list", "deploy.site", "whoami"} <= ids


def test_the_catalogue_is_untouched_when_execution_is_on():
    ids = {c["id"] for c in capability_table()}
    assert EXEC_IDS <= ids


def test_the_mcp_tool_list_drops_the_tools_that_cannot_work(publish_only):
    names = {t["name"] for t in mcp_server.available_tools()}
    assert not (names & EXEC_TOOLS)
    assert "write_file" in names and "list_files" in names


def test_the_mcp_instructions_say_it_does_not_execute(publish_only):
    text = mcp_server.server_instructions()
    assert "does NOT run code" in text
    assert "public/" in text
    assert "visitor's browser" in text


async def test_the_agent_card_only_lists_skills_that_work(client, publish_only):
    card = (await client.get("/.well-known/agent-card.json")).json()
    assert not ({s["id"] for s in card["skills"]} & EXEC_TOOLS)


async def test_the_manual_does_not_document_execution(client, publish_only):
    manual = (await client.get("/llms.txt")).text
    assert "run_code" not in manual


# --------------------------------------------------------------------------
# What happens when an agent asks anyway
# --------------------------------------------------------------------------
async def test_asking_to_run_code_is_taught_not_stonewalled(client, account, publish_only):
    """An agent that assumes a computer must learn fast, or it writes a server."""
    res = await client.post(
        "/mcp",
        headers={**account["headers"], "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "run_code", "arguments": {"code": "print(1)"}}},
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "execution_unavailable" in text
    assert "visitor's browser" in text
    assert "public/" in text


async def test_the_rest_exec_endpoint_refuses_with_a_fix(client, account, publish_only):
    res = await client.post(
        "/api/v1/exec", json={"code": "print(1)"}, headers=account["headers"]
    )
    assert res.status_code == 501
    error = res.json()["error"]
    assert error["code"] == "execution_unavailable"
    assert "browser" in error["fix"]


async def test_deploying_a_service_refuses_with_a_fix(client, account, publish_only):
    h = account["headers"]
    await client.put("/api/v1/files?path=app.py", json={"content": "x = 1"}, headers=h)
    res = await client.post(
        "/api/v1/deployments",
        json={"name": "api", "kind": "service", "command": "python app.py", "port": 8080},
        headers=h,
    )
    assert res.status_code == 501
    assert res.json()["error"]["code"] == "execution_unavailable"


# --------------------------------------------------------------------------
# What still works, because it is the entire product
# --------------------------------------------------------------------------
async def test_publishing_still_works_end_to_end(client, account, publish_only):
    res = await client.put(
        "/api/v1/files?path=public/index.html",
        json={"content": "<!doctype html><title>Meme</title><h1>still live</h1>"},
        headers=account["headers"],
    )
    assert res.status_code == 200

    page = await client.get(f"/u/{account['username']}/")
    assert page.status_code == 200
    assert "still live" in page.text


async def test_writes_stop_before_the_host_disk_does(client, account, monkeypatch):
    """Per-user quotas cap one workspace, never their sum.

    A disk at 100% does not just reject writes — it corrupts the database and
    the whole space stops answering, for everyone, at once.
    """
    from collections import namedtuple

    from app import quotas

    full = namedtuple("Usage", "total used free")(75_000_000_000, 75_000_000_000, 0)
    monkeypatch.setattr(quotas.shutil, "disk_usage", lambda _p: full)

    res = await client.put(
        "/api/v1/files?path=public/index.html",
        json={"content": "<h1>hi</h1>"},
        headers=account["headers"],
    )
    assert res.status_code == 507
    error = res.json()["error"]
    assert error["code"] == "host_disk_full"
    # It must not read as the agent's fault, or it retries forever.
    assert "retrying will not help" in error["fix"]


async def test_a_browser_app_is_served_whole(client, account, publish_only):
    """What replaces server-side execution: things that run on the visitor."""
    h = account["headers"]
    for path, content in [
        ("public/index.html", "<title>Tool</title><script src='app.js'></script>"),
        ("public/app.js", "document.title = 'ready'"),
    ]:
        await client.put(f"/api/v1/files?path={path}", json={"content": content}, headers=h)

    base = f"/u/{account['username']}"
    assert "app.js" in (await client.get(f"{base}/")).text
    assert (await client.get(f"{base}/app.js")).headers["content-type"].startswith(
        "text/javascript"
    )
