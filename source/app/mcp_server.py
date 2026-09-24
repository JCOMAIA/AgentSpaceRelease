"""MCP door: /mcp — Streamable HTTP transport, protocol 2025-06-18.

Hand-written rather than pulled from a framework because authentication here is
per-user-API-key and every tool result carries our teaching envelope; both are
easier to get exactly right with the JSON-RPC layer in view.

Responses use `application/json`, which the Streamable HTTP spec permits for
servers that do not need to push server-initiated messages.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from . import operations, storage
from .db import get_session
from .deps import KEY_HEADERS, auth_headers_seen, bearer_token, resolve_api_key
from .models import User
from .teaching import AgentSpaceError, Guide, onboarding_markdown

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26", "2024-11-05"}

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_INSTRUCTIONS_WITH_EXEC = """\
You are connected to AgentSpace: a persistent workspace, a code sandbox, and public hosting.

What is here:
  * A private filesystem that survives across sessions. Sandbox runs mount it at /workspace.
  * `run_code` executes python, node or bash against that filesystem in an isolated container.
  * `deploy_site` and `deploy_service` put what you build on a public URL.

How to work here:
  1. Call `whoami` first — it reports your quota and your public URLs.
  2. Use `list_files` before writing, so you do not overwrite your own earlier work.
  3. Every result ends with a "WHAT YOU CAN DO NEXT" section. It is not decoration;
     it names the exact tool and arguments for the next step.
  4. Every error names the fix. Read it before retrying, and do not retry unchanged.

Paths are always relative to your workspace root. `..` is rejected.
Sandboxes have no network access unless the account's plan enables egress.
"""

_INSTRUCTIONS_PUBLISH_ONLY = """\
You are connected to AgentSpace: a persistent workspace that is also a public website.

What is here:
  * A private filesystem that survives across sessions.
  * Anything you write under `public/` is live at your public URL immediately.
    There is no deploy step, no build, and no publish call.

This space does NOT run code on the server. Build things that run in
the visitor's browser — HTML, CSS and JavaScript — which covers pages, tools,
dashboards, visualisations, galleries and games. Do not write a server and wait
for it to start; nothing will start it.

How to work here:
  1. Call `whoami` first — it reports your quota and your public URLs.
  2. Use `list_files` before writing, so you do not overwrite your own earlier work.
  3. Write to `public/index.html`, then open your public URL. That is the whole loop.
  4. Every error names the fix. Read it before retrying, and do not retry unchanged.

Paths are always relative to your workspace root. `..` is rejected.
"""


def server_instructions() -> str:
    from .teaching import execution_enabled

    return _INSTRUCTIONS_WITH_EXEC if execution_enabled() else _INSTRUCTIONS_PUBLISH_ONLY

_STRING = {"type": "string"}


def _tool(name: str, title: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "whoami", "Inspect your space",
        "Report your username, plan, quota usage, live deployments and public URLs. "
        "Safe, read-only, and the right first call in any new session.",
        {}, [],
    ),
    _tool(
        "list_files", "List files",
        "List entries under a directory of your persistent workspace. "
        "Use '.' for the root. Call this before writing to avoid clobbering earlier work.",
        {"path": {**_STRING, "description": "Directory relative to your root.", "default": "."}},
        [],
    ),
    _tool(
        "read_file", "Read a file",
        "Return a file's contents. Text files come back as UTF-8; anything else as base64.",
        {"path": {**_STRING, "description": "File path relative to your root."}},
        ["path"],
    ),
    _tool(
        "write_file", "Write a file",
        "Create or overwrite a file. Parent directories are created for you. "
        "This is how you put anything into your space.",
        {
            "path": {**_STRING, "description": "Destination path relative to your root."},
            "content": {**_STRING, "description": "The contents to write."},
            "encoding": {
                "type": "string", "enum": ["utf-8", "base64"], "default": "utf-8",
                "description": "Use base64 for binary files.",
            },
        },
        ["path", "content"],
    ),
    _tool(
        "delete_path", "Delete a path",
        "Remove a file, or a directory and everything under it. Not reversible.",
        {"path": {**_STRING, "description": "Path to remove."}}, ["path"],
    ),
    _tool(
        "move_path", "Move or rename",
        "Move a file or directory to a new path within your workspace.",
        {"from": {**_STRING}, "to": {**_STRING}}, ["from", "to"],
    ),
    _tool(
        "run_code", "Run code in the sandbox",
        "Execute code in an isolated container with your workspace mounted at /workspace "
        "and set as the working directory. Returns stdout, stderr and the exit code. "
        "Files your code writes under /workspace persist; everything else is discarded.",
        {
            "code": {**_STRING, "description": "The program to run."},
            "language": {
                "type": "string", "enum": ["python", "node", "bash"], "default": "python",
            },
            "timeout_s": {
                "type": "integer",
                "description": "Seconds before the run is killed. Capped by your plan.",
            },
        },
        ["code"],
    ),
    _tool(
        "deploy_site", "Publish a static website",
        "Serve a workspace directory publicly. Put an index.html in it first. "
        "After this, edits to those files are live immediately — no redeploy needed.",
        {
            "name": {**_STRING, "description": "URL slug, e.g. 'my-site'. Lowercase and hyphens."},
            "source_dir": {**_STRING, "default": ".", "description": "Directory to serve."},
        },
        ["name"],
    ),
    _tool(
        "deploy_service", "Publish a running service",
        "Start a long-lived container running your command and reverse-proxy public traffic "
        "to it. Your process must listen on 0.0.0.0 at the port you declare.",
        {
            "name": {**_STRING, "description": "URL slug for the service."},
            "command": {**_STRING, "description": "Shell command, e.g. 'python server.py'."},
            "port": {"type": "integer", "description": "Port your process listens on."},
            "source_dir": {**_STRING, "default": ".", "description": "Mounted at /app."},
        },
        ["name", "command", "port"],
    ),
    _tool(
        "list_deployments", "List deployments",
        "Show everything you have published, with status and public URLs.", {}, [],
    ),
    _tool(
        "deployment_logs", "Read service logs",
        "Tail stdout and stderr of a running service. The first thing to check when a "
        "deployed service does not respond.",
        {
            "name": {**_STRING},
            "tail": {"type": "integer", "default": 200, "description": "Lines to return."},
        },
        ["name"],
    ),
    _tool(
        "delete_deployment", "Remove a deployment",
        "Take a site or service offline. The underlying files stay in your workspace.",
        {"name": {**_STRING}}, ["name"],
    ),
]

# Tools that need a sandbox behind them. A publish-only space hides these rather
# than offering a tool whose only possible outcome is a refusal.
EXECUTION_TOOLS = {"run_code", "deploy_service", "deployment_logs"}


def available_tools() -> list[dict[str, Any]]:
    from .teaching import execution_enabled

    if execution_enabled():
        return TOOLS
    return [t for t in TOOLS if t["name"] not in EXECUTION_TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------
def _result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def render_guide(guide: Guide) -> str:
    """Turn a Guide into the text an agent reads at the end of a tool result."""
    lines = [f"\n— {guide.you_are_here}"]
    if guide.next_steps:
        lines.append("\nWHAT YOU CAN DO NEXT:")
        for i, step in enumerate(guide.next_steps, 1):
            call = step.call
            if call.get("transport") == "rest":
                how = f"{call.get('method')} {call.get('path')}"
                if "body" in call:
                    how += f"  body={json.dumps(call['body'], ensure_ascii=False)}"
            elif call.get("transport") == "mcp":
                how = f"tool `{call.get('tool')}` args={json.dumps(call.get('arguments', {}))}"
            elif "url" in call:
                how = str(call["url"])
            else:
                how = json.dumps(call, ensure_ascii=False)
            reason = f" — {step.why}" if step.why else ""
            lines.append(f"  {i}. {step.do}{reason}\n     → {how}")
    for note in guide.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def _summarize(operation: str, data: Any) -> str:
    """A short human/agent-readable rendering of the payload."""
    if operation == "list_files" and isinstance(data, dict):
        entries = data.get("entries", [])
        if not entries:
            return f"{data.get('path')!r} is empty."
        rows = "\n".join(
            f"  {'DIR ' if e['type'] == 'dir' else 'FILE'}  {e['path']}"
            + ("" if e["type"] == "dir" else f"  ({e['size']} B)")
            for e in entries
        )
        return f"{len(entries)} entries under {data.get('path')!r}:\n{rows}"
    if operation == "read_file" and isinstance(data, dict):
        return f"{data['path']} ({data['size']} B, {data['encoding']}):\n{data['content']}"
    if operation == "run_code" and isinstance(data, dict):
        parts = [f"exit_code={data['exit_code']}  duration={data['duration_ms']}ms"]
        if data.get("stdout"):
            parts.append(f"--- stdout ---\n{data['stdout'].rstrip()}")
        if data.get("stderr"):
            parts.append(f"--- stderr ---\n{data['stderr'].rstrip()}")
        if not data.get("stdout") and not data.get("stderr"):
            parts.append("(the program produced no output)")
        return "\n".join(parts)
    if operation == "deployment_logs" and isinstance(data, dict):
        return data.get("logs") or "(no log output yet)"
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


async def _authenticate(request: Request, session: AsyncSession) -> User:
    token = bearer_token(request)
    if not token:
        # Say what arrived, not just what was wanted. A hosted connector's setup
        # dialog offers a menu of header names, and someone who picked one and
        # got a bare "needs a key" has no way to tell whether the key never
        # left, landed under a name we ignore, or is simply wrong.
        seen = auth_headers_seen(request)
        where = (f"Your request did carry {', '.join(seen)}, but no usable key was in it. "
                 if seen else "Your request carried no credential header at all. ")
        raise AgentSpaceError(
            "unauthenticated",
            "This MCP server requires an AgentSpace API key.",
            where + "Send the key as `Authorization: Bearer ask_...`, or under any of "
            f"`{'`, `'.join(KEY_HEADERS)}`. Then reconnect the client.",
            status_code=401,
            details={"headers_seen": seen, "accepted": ["authorization", *KEY_HEADERS]},
        )
    user = await resolve_api_key(session, token)
    if user is None:
        raise AgentSpaceError(
            "invalid_api_key",
            "That API key is not valid or has been revoked.",
            "Ask the account owner for a fresh key from the dashboard.",
            status_code=401,
        )
    from .deps import ensure_not_suspended, ensure_workspace

    ensure_not_suspended(user)
    await ensure_workspace(session, user)
    return user


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------
@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request, session: SessionDep) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return _json(_error(None, -32700, "Parse error: body was not valid JSON"), 400)

    # Batches were removed in the 2025-06-18 revision but older clients still send them.
    if isinstance(payload, list):
        replies = [r for r in [await _handle(msg, request, session) for msg in payload] if r]
        return _json(replies, 200) if replies else Response(status_code=202)

    reply = await _handle(payload, request, session)
    if reply is None:
        return Response(status_code=202)
    return _json(reply, 200)


@router.get("/mcp", include_in_schema=False)
async def mcp_sse_not_supported() -> Response:
    # Spec: a server without a server-initiated stream answers GET with 405.
    return Response(status_code=405, headers={"Allow": "POST"})


def _json(body: Any, status: int) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False, default=str),
        media_type="application/json",
        status_code=status,
    )


async def _handle(msg: dict, request: Request, session: AsyncSession) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request: expected a JSON-RPC 2.0 object")

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        requested = params.get("protocolVersion", PROTOCOL_VERSION)
        version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return _result(
            req_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {}},
                "serverInfo": {
                    "name": "agentspace",
                    "title": "AgentSpace",
                    "version": "0.1.0",
                },
                "instructions": server_instructions(),
            },
        )

    if is_notification:
        return None  # initialized / cancelled / progress — nothing to answer

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": available_tools()})

    if method == "prompts/list":
        return _result(req_id, {"prompts": []})

    if method == "resources/list":
        try:
            user = await _authenticate(request, session)
        except AgentSpaceError as exc:
            return _error(req_id, -32001, exc.message, exc.as_dict()["error"])
        resources = [
            {
                "uri": "agentspace://manual",
                "name": "manual",
                "title": "How to use AgentSpace",
                "description": "The full operating manual for this space, written for agents.",
                "mimeType": "text/markdown",
            }
        ]
        for entry in storage.list_dir(user.id, "."):
            resources.append(
                {
                    "uri": f"agentspace://workspace/{entry['path']}",
                    "name": entry["path"],
                    "description": f"{entry['type']} in your workspace",
                    "mimeType": "text/plain" if entry["type"] == "file" else "inode/directory",
                }
            )
        return _result(req_id, {"resources": resources})

    if method == "resources/read":
        uri = params.get("uri", "")
        if uri == "agentspace://manual":
            return _result(
                req_id,
                {"contents": [{"uri": uri, "mimeType": "text/markdown",
                               "text": onboarding_markdown()}]},
            )
        if uri.startswith("agentspace://workspace/"):
            try:
                user = await _authenticate(request, session)
                rel_path = uri.split("agentspace://workspace/", 1)[1]
                data, _ = await operations.read_file(user, rel_path)
            except AgentSpaceError as exc:
                return _error(req_id, -32002, exc.message, exc.as_dict()["error"])
            return _result(
                req_id,
                {"contents": [{"uri": uri, "mimeType": "text/plain", "text": data["content"]}]},
            )
        return _error(req_id, -32602, f"Unknown resource URI: {uri!r}")

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            user = await _authenticate(request, session)
            data, guide = await operations.dispatch(session, user, name, arguments)
            await session.commit()
        except AgentSpaceError as exc:
            # Tool-level failures are results, not protocol errors: this is what
            # lets the model see the `fix` text and correct itself.
            return _result(
                req_id,
                {
                    "content": [{"type": "text", "text": _render_error(exc)}],
                    "structuredContent": exc.as_dict()["error"],
                    "isError": True,
                },
            )
        except Exception as exc:  # pragma: no cover - unexpected
            return _result(
                req_id,
                {
                    "content": [{"type": "text", "text": f"Internal error: {exc}"}],
                    "isError": True,
                },
            )
        text = _summarize(name, data) + render_guide(guide)
        return _result(
            req_id,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": data if isinstance(data, dict) else {"result": data},
                "isError": False,
            },
        )

    return _error(
        req_id, -32601,
        f"Method {method!r} is not implemented by this server",
        {"supported": ["initialize", "ping", "tools/list", "tools/call",
                       "resources/list", "resources/read"]},
    )


def _render_error(exc: AgentSpaceError) -> str:
    lines = [f"ERROR [{exc.code}]: {exc.message}", f"FIX: {exc.fix}"]
    if exc.try_this:
        lines.append(f"TRY: {json.dumps(exc.try_this, ensure_ascii=False)}")
    if exc.details:
        lines.append(f"DETAILS: {json.dumps(exc.details, ensure_ascii=False, default=str)}")
    return "\n".join(lines)
