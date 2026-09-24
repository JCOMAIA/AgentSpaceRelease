"""The promise that makes this product: an agent can learn the space from the space."""

from __future__ import annotations


async def test_hello_is_public_and_explains_how_to_get_access(client):
    res = await client.get("/api/v1/hello")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "how_to_get_access" in body["data"]
    assert body["data"]["doors"].keys() == {"rest", "mcp", "a2a"}
    assert body["data"]["capabilities"]


async def test_hello_personalises_once_authenticated(client, account):
    res = await client.get("/api/v1/hello", headers=account["headers"])
    body = res.json()["data"]
    assert "how_to_get_access" not in body
    assert body["your_plan"]["name"] == "free"
    assert account["username"] in body["your_space"]["urls"]["path"]


async def test_llms_txt_is_served_as_plain_text(client):
    res = await client.get("/llms.txt")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "AgentSpace" in res.text
    assert "Authorization: Bearer" in res.text


async def test_discovery_document_lists_every_door(client):
    res = await client.get("/.well-known/agentspace.json")
    endpoints = res.json()["endpoints"]
    for key in ("rest", "mcp", "a2a", "agent_card", "manual"):
        assert key in endpoints


async def test_unauthenticated_error_teaches_recovery(client):
    res = await client.get("/api/v1/whoami")
    assert res.status_code == 401
    error = res.json()["error"]
    assert error["code"] == "unauthenticated"
    assert "Authorization: Bearer" in error["fix"]
    assert error["try_this"]["path"] == "/api/v1/hello"


async def test_unknown_endpoint_returns_the_catalogue(client, account):
    res = await client.get("/api/v1/nonsense", headers=account["headers"])
    assert res.status_code == 404
    error = res.json()["error"]
    assert error["code"] == "unknown_endpoint"
    assert len(error["details"]["capabilities"]) > 5


async def test_every_error_carries_a_fix(client, account):
    """The core invariant of the teaching layer."""
    failures = [
        await client.get("/api/v1/files?path=../../etc", headers=account["headers"]),
        await client.get("/api/v1/files/content?path=missing.txt", headers=account["headers"]),
        await client.post(
            "/api/v1/exec",
            json={"language": "python", "code": "x"},
            headers={"Authorization": "Bearer ask_bogus_key"},
        ),
    ]
    for res in failures:
        assert res.status_code >= 400
        error = res.json()["error"]
        assert error.get("fix"), f"missing fix on {error}"
