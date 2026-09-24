"""MCP and A2A conformance, plus the shared-state guarantee across doors."""

from __future__ import annotations


def rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------
async def test_mcp_initialize_returns_instructions(client, account):
    res = await client.post(
        "/mcp",
        json=rpc("initialize", {"protocolVersion": "2025-06-18",
                                "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}),
        headers=account["headers"],
    )
    result = res.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "agentspace"
    # The instructions field is where a fresh agent learns the space.
    assert "workspace" in result["instructions"].lower()
    assert "whoami" in result["instructions"]


async def test_mcp_initialize_negotiates_older_protocols(client, account):
    res = await client.post(
        "/mcp", json=rpc("initialize", {"protocolVersion": "2024-11-05"}),
        headers=account["headers"],
    )
    assert res.json()["result"]["protocolVersion"] == "2024-11-05"


async def test_mcp_notification_gets_202_and_no_body(client, account):
    res = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=account["headers"],
    )
    assert res.status_code == 202
    assert res.content == b""


async def test_mcp_tools_have_complete_schemas(client, account):
    res = await client.post("/mcp", json=rpc("tools/list"), headers=account["headers"])
    tools = res.json()["result"]["tools"]
    assert {"whoami", "run_code", "write_file", "deploy_site"} <= {t["name"] for t in tools}
    for tool in tools:
        assert tool["description"].strip()
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        for required in schema["required"]:
            assert required in schema["properties"], f"{tool['name']}: {required} undeclared"


async def test_mcp_get_is_405(client):
    res = await client.get("/mcp")
    assert res.status_code == 405
    assert res.headers["allow"] == "POST"


async def test_mcp_requires_authentication(client):
    res = await client.post("/mcp", json=rpc("tools/call", {"name": "whoami", "arguments": {}}))
    result = res.json()["result"]
    assert result["isError"] is True
    assert "FIX:" in result["content"][0]["text"]


async def test_mcp_tool_call_appends_next_steps(client, account):
    res = await client.post(
        "/mcp", json=rpc("tools/call", {"name": "whoami", "arguments": {}}),
        headers=account["headers"],
    )
    result = res.json()["result"]
    assert result["isError"] is False
    assert "WHAT YOU CAN DO NEXT" in result["content"][0]["text"]
    assert result["structuredContent"]["username"] == account["username"]


async def test_mcp_tool_error_is_a_result_not_a_protocol_error(client, account):
    """Errors must reach the model as readable text so it can self-correct."""
    res = await client.post(
        "/mcp", json=rpc("tools/call", {"name": "read_file", "arguments": {"path": "nope.txt"}}),
        headers=account["headers"],
    )
    body = res.json()
    assert "error" not in body
    result = body["result"]
    assert result["isError"] is True
    assert "FIX:" in result["content"][0]["text"]


async def test_mcp_missing_argument_names_the_argument(client, account):
    res = await client.post(
        "/mcp", json=rpc("tools/call", {"name": "read_file", "arguments": {}}),
        headers=account["headers"],
    )
    result = res.json()["result"]
    assert result["structuredContent"]["details"]["missing"] == "path"


async def test_mcp_resources_expose_the_manual(client, account):
    res = await client.post("/mcp", json=rpc("resources/list"), headers=account["headers"])
    uris = [r["uri"] for r in res.json()["result"]["resources"]]
    assert "agentspace://manual" in uris

    res = await client.post(
        "/mcp", json=rpc("resources/read", {"uri": "agentspace://manual"}),
        headers=account["headers"],
    )
    assert "AgentSpace" in res.json()["result"]["contents"][0]["text"]


async def test_mcp_unknown_method_lists_supported_ones(client, account):
    res = await client.post("/mcp", json=rpc("does/not/exist"), headers=account["headers"])
    error = res.json()["error"]
    assert error["code"] == -32601
    assert "tools/call" in error["data"]["supported"]


# --------------------------------------------------------------------------
# A2A
# --------------------------------------------------------------------------
async def test_agent_card_is_public_and_well_formed(client):
    res = await client.get("/.well-known/agent-card.json")
    card = res.json()
    assert card["name"] == "AgentSpace"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["url"].endswith("/a2a")
    assert card["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert card["skills"]
    for skill in card["skills"]:
        assert {"id", "name", "description", "tags", "examples"} <= skill.keys()


async def test_legacy_agent_card_path_still_resolves(client):
    res = await client.get("/.well-known/agent.json")
    assert res.status_code == 200
    assert res.json()["name"] == "AgentSpace"


async def test_a2a_requires_auth_and_points_at_the_card(client):
    res = await client.post(
        "/a2a",
        json=rpc("message/send", {"message": {"role": "user", "messageId": "m1",
                                              "parts": [{"kind": "text", "text": "whoami"}]}}),
    )
    assert res.status_code == 401
    assert "agent-card.json" in res.json()["error"]["data"]["fix"]


async def test_a2a_structured_invocation(client, account):
    res = await client.post(
        "/a2a",
        json=rpc(
            "message/send",
            {
                "message": {
                    "role": "user",
                    "messageId": "m1",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {"operation": "run_code",
                                     "arguments": {"code": "print('from a2a')"}},
                        }
                    ],
                }
            },
        ),
        headers=account["headers"],
    )
    task = res.json()["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    text = task["history"][-1]["parts"][0]["text"]
    assert "from a2a" in text
    assert task["artifacts"][0]["parts"][0]["data"]["exit_code"] == 0


async def test_a2a_text_shorthand(client, account):
    res = await client.post(
        "/a2a",
        json=rpc("message/send", {"message": {"role": "user", "messageId": "m2",
                                              "parts": [{"kind": "text",
                                                         "text": "run_code: print(6*7)"}]}}),
        headers=account["headers"],
    )
    task = res.json()["result"]
    assert task["status"]["state"] == "completed"
    assert "42" in task["history"][-1]["parts"][0]["text"]


async def test_a2a_ambiguous_message_teaches_the_precise_form(client, account):
    res = await client.post(
        "/a2a",
        json=rpc("message/send", {"message": {"role": "user", "messageId": "m3",
                                              "parts": [{"kind": "text",
                                                         "text": "please do something nice"}]}}),
        headers=account["headers"],
    )
    task = res.json()["result"]
    assert task["status"]["state"] == "input-required"
    guidance = task["history"][-1]["parts"][0]["text"]
    assert '"operation"' in guidance
    assert "run_code" in guidance


async def test_a2a_task_is_retrievable_afterwards(client, account):
    res = await client.post(
        "/a2a",
        json=rpc("message/send", {"message": {"role": "user", "messageId": "m4",
                                              "parts": [{"kind": "text", "text": "whoami"}]}}),
        headers=account["headers"],
    )
    task_id = res.json()["result"]["id"]

    res = await client.post("/a2a", json=rpc("tasks/get", {"id": task_id}),
                            headers=account["headers"])
    assert res.json()["result"]["id"] == task_id


async def test_a2a_cannot_read_another_accounts_task(client, account):
    res = await client.post(
        "/a2a",
        json=rpc("message/send", {"message": {"role": "user", "messageId": "m5",
                                              "parts": [{"kind": "text", "text": "whoami"}]}}),
        headers=account["headers"],
    )
    task_id = res.json()["result"]["id"]

    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "nosy", "email": "nosy@example.com", "password": "long-enough-pass"},
    )
    other = {"Authorization": f"Bearer {reg.json()['data']['api_key']}"}

    res = await client.post("/a2a", json=rpc("tasks/get", {"id": task_id}), headers=other)
    assert res.json()["error"]["message"] == "Task not found"


# --------------------------------------------------------------------------
# The doors share one space
# --------------------------------------------------------------------------
async def test_write_over_mcp_read_over_rest(client, account):
    h = account["headers"]
    await client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "write_file",
                                "arguments": {"path": "shared.txt", "content": "one space"}}),
        headers=h,
    )
    res = await client.get("/api/v1/files/content?path=shared.txt", headers=h)
    assert res.json()["data"]["content"] == "one space"

    res = await client.post(
        "/a2a",
        json=rpc("message/send", {"message": {"role": "user", "messageId": "m6",
                                              "parts": [{"kind": "text",
                                                         "text": "read_file: shared.txt"}]}}),
        headers=h,
    )
    assert "one space" in res.json()["result"]["history"][-1]["parts"][0]["text"]
