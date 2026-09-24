"""Files, sandbox execution, quotas and path safety."""

from __future__ import annotations

import base64

import pytest


async def test_write_read_list_delete_roundtrip(client, account):
    h = account["headers"]

    res = await client.put(
        "/api/v1/files?path=notes/hello.txt", json={"content": "hi there"}, headers=h
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["size"] == 8

    res = await client.get("/api/v1/files/content?path=notes/hello.txt", headers=h)
    assert res.json()["data"]["content"] == "hi there"

    res = await client.get("/api/v1/files?path=notes", headers=h)
    assert [e["path"] for e in res.json()["data"]["entries"]] == ["notes/hello.txt"]

    res = await client.delete("/api/v1/files?path=notes", headers=h)
    assert res.json()["data"]["deleted"] is True

    res = await client.get("/api/v1/files/content?path=notes/hello.txt", headers=h)
    assert res.status_code == 404


@pytest.mark.parametrize("name", ["README", "Dockerfile", "Makefile", ".gitignore"])
async def test_extensionless_text_files_come_back_readable(client, account, name):
    """Agents work with these constantly; base64 would make them unreadable."""
    h = account["headers"]
    await client.put(f"/api/v1/files?path={name}", json={"content": "plain text"}, headers=h)
    res = await client.get(f"/api/v1/files/content?path={name}", headers=h)
    assert res.json()["data"]["encoding"] == "utf-8"
    assert res.json()["data"]["content"] == "plain text"


async def test_binary_roundtrip_via_base64(client, account):
    h = account["headers"]
    blob = bytes(range(256))
    res = await client.put(
        "/api/v1/files?path=blob.bin",
        json={"content": base64.b64encode(blob).decode(), "encoding": "base64"},
        headers=h,
    )
    assert res.status_code == 200
    raw = await client.get("/api/v1/files/raw?path=blob.bin", headers=h)
    assert raw.content == blob


@pytest.mark.parametrize(
    "path", ["../escape.txt", "../../etc/passwd", "notes/../../out.txt", "a/b/../../../x"]
)
async def test_path_traversal_is_refused(client, account, path):
    res = await client.put(f"/api/v1/files?path={path}", json={"content": "x"},
                           headers=account["headers"])
    assert res.status_code in (403, 404)
    assert res.json()["error"]["code"] in ("path_escape", "not_found")


@pytest.mark.parametrize(
    ("sent", "lands_at"),
    [
        ("/etc/passwd", "etc/passwd"),
        ("/workspace/app.py", "app.py"),
        ("/workspace/src/main.py", "src/main.py"),
        ("/top.txt", "top.txt"),
    ],
)
async def test_absolute_paths_are_confined_to_the_workspace(client, account, sent, lands_at):
    """Chroot semantics: absolute paths resolve inside the workspace, never above it.

    `/workspace/...` is the prefix we advertise to agents, so echoing it back must work.
    """
    h = account["headers"]
    res = await client.put(f"/api/v1/files?path={sent}", json={"content": "confined"}, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["path"] == lands_at

    res = await client.get(f"/api/v1/files/content?path={lands_at}", headers=h)
    assert res.json()["data"]["content"] == "confined"


async def test_workspaces_are_isolated_between_accounts(client, account):
    """The load-bearing multi-tenancy check."""
    h1 = account["headers"]
    await client.put("/api/v1/files?path=secret.txt", json={"content": "private"}, headers=h1)

    res = await client.post(
        "/api/v1/account/register",
        json={"username": "intruder", "email": "intruder@example.com",
              "password": "another-long-password"},
    )
    h2 = {"Authorization": f"Bearer {res.json()['data']['api_key']}"}

    listing = await client.get("/api/v1/files?path=.", headers=h2)
    assert listing.json()["data"]["entries"] == []

    peek = await client.get("/api/v1/files/content?path=secret.txt", headers=h2)
    assert peek.status_code == 404


async def test_writing_under_public_says_it_is_already_live(client, account):
    """The guide after a write is the most-read sentence in the product.

    It used to say "publish the directory containing this file" and "run it" —
    one unnecessary because public/ is already served, the other impossible
    where nothing executes. A connected agent read both and reported them back
    to its user as steps it had been told to take.
    """
    res = await client.put(
        "/api/v1/files?path=public/index.html", json={"content": "<h1>hi</h1>"},
        headers=account["headers"],
    )
    body = res.json()
    assert body["data"]["live"] is True
    assert body["data"]["url"].endswith(f"/@{account['username']}/")

    steps = body["guide"]["next_steps"]
    assert any("Open it" in s["do"] for s in steps)
    assert not any("deployments" in str(s["call"]) for s in steps), (
        "still telling the agent to create a deployment it does not need"
    )


async def test_writing_outside_public_says_it_is_not_on_the_web(client, account):
    res = await client.put(
        "/api/v1/files?path=notes.md", json={"content": "rascunho"},
        headers=account["headers"],
    )
    body = res.json()
    assert body["data"]["live"] is False
    assert "url" not in body["data"]
    assert any("not on the web" in n for n in body["guide"]["notes"])


async def test_exec_runs_code_and_sees_the_workspace(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=data.txt", json={"content": "42"}, headers=h)

    res = await client.post(
        "/api/v1/exec",
        json={"language": "python", "code": "print(open('data.txt').read())"},
        headers=h,
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["exit_code"] == 0
    assert "42" in data["stdout"]


async def test_exec_reports_failure_without_hiding_it(client, account):
    res = await client.post(
        "/api/v1/exec",
        json={"language": "python", "code": "raise ValueError('boom')"},
        headers=account["headers"],
    )
    data = res.json()["data"]
    assert data["exit_code"] != 0
    assert "boom" in data["stderr"]
    assert "stderr" in res.json()["guide"]["next_steps"][0]["why"].lower()


async def test_exec_writes_persist_in_the_workspace(client, account):
    h = account["headers"]
    await client.post(
        "/api/v1/exec",
        json={"language": "python", "code": "open('made-by-code.txt','w').write('ok')"},
        headers=h,
    )
    res = await client.get("/api/v1/files/content?path=made-by-code.txt", headers=h)
    assert res.json()["data"]["content"] == "ok"


async def test_unsupported_language_lists_the_supported_ones(client, account):
    res = await client.post(
        "/api/v1/exec", json={"language": "cobol", "code": "x"}, headers=account["headers"]
    )
    assert res.status_code == 422  # rejected by the request schema


async def test_deployment_quota_is_enforced_with_a_usable_message(client, account):
    h = account["headers"]
    await client.put("/api/v1/files?path=index.html", json={"content": "<p>a</p>"}, headers=h)

    from app.config import PLANS

    allowed = PLANS["free"].max_deployments
    for i in range(allowed):
        res = await client.post(
            "/api/v1/deployments",
            json={"name": f"site{i}", "kind": "static", "source_dir": "."},
            headers=h,
        )
        assert res.status_code == 200, res.text

    res = await client.post(
        "/api/v1/deployments",
        json={"name": "one-too-many", "kind": "static", "source_dir": "."},
        headers=h,
    )
    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "quota_deployments"
    assert "Delete an existing deployment" in error["fix"]


async def test_invalid_deployment_name_explains_the_rule(client, account):
    res = await client.post(
        "/api/v1/deployments", json={"name": "Not A Slug!", "kind": "static"},
        headers=account["headers"],
    )
    assert res.status_code == 400
    assert "lowercase" in res.json()["error"]["fix"]
