"""A2A door: agent card + JSON-RPC at /a2a.

AgentSpace is not a language model, so it does not guess at free-form prose. A
peer agent invokes a skill precisely by sending a DataPart:

    {"kind": "data", "data": {"operation": "run_code",
                              "arguments": {"code": "print(1)"}}}

Text also works for the obvious shapes (`run_code: print(1)`). When a message
cannot be resolved, the task lands in `input-required` and the reply spells out
the exact payload that would have worked — the same teach-on-failure contract
the other two doors follow.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from . import operations
from .config import get_settings
from .db import get_session
from .deps import bearer_token, ensure_not_suspended, ensure_workspace, resolve_api_key
from .mcp_server import _summarize, render_guide
from .models import AgentTask, User
from .teaching import CAPABILITIES, AgentSpaceError, available_capabilities, execution_enabled

router = APIRouter(tags=["a2a"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]

A2A_PROTOCOL_VERSION = "0.3.0"

# operation -> (skill id, example utterances)
SKILL_EXAMPLES: dict[str, list[str]] = {
    "whoami": ["whoami", "what is my quota"],
    "list_files": ["list_files: .", "show me my files"],
    "read_file": ["read_file: notes.md"],
    "write_file": ['{"operation":"write_file","arguments":{"path":"a.txt","content":"hi"}}'],
    "run_code": ["run_code: print(6*7)"],
    "deploy_site": ['{"operation":"deploy_site","arguments":{"name":"site","source_dir":"www"}}'],
    "list_deployments": ["list_deployments"],
}


def agent_card() -> dict[str, Any]:
    s = get_settings()
    skills = []
    for cap in available_capabilities():
        op = cap.mcp_tool
        if op is None:
            continue
        skills.append(
            {
                "id": op,
                "name": cap.title,
                "description": cap.description,
                "tags": list(cap.tags),
                "examples": SKILL_EXAMPLES.get(op, [f"{op}"]),
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["text/plain", "application/json"],
            }
        )

    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "AgentSpace",
        "description": (
            "A persistent workspace, code sandbox and public hosting for agents. "
            "Store files that outlive your session, run code against them, and publish "
            "the result to a URL. Every reply explains what you can do next."
        ),
        "url": f"{s.public_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": "0.1.0",
        "documentationUrl": f"{s.public_url}/llms.txt",
        "provider": {"organization": "AgentSpace", "url": s.public_url},
        "iconUrl": f"{s.public_url}/static/icon.svg",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "An AgentSpace API key (ask_...). A human issues it from the "
                "dashboard; it is the same credential the REST and MCP doors take.",
            }
        },
        "security": [{"bearerAuth": []}],
        "skills": skills,
    }


@router.get("/.well-known/agent-card.json", include_in_schema=False)
async def well_known_agent_card() -> dict[str, Any]:
    return agent_card()


@router.get("/.well-known/agent.json", include_in_schema=False)
async def legacy_agent_card() -> dict[str, Any]:
    """Older A2A clients look here. Same document."""
    return agent_card()


# --------------------------------------------------------------------------
# Message parsing
# --------------------------------------------------------------------------
KNOWN_OPS = {c.mcp_tool for c in CAPABILITIES if c.mcp_tool}
_PREFIX_RE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.*)$", re.DOTALL)


def parse_message(message: dict) -> tuple[str | None, dict[str, Any], str]:
    """Return `(operation, arguments, raw_text)`."""
    parts = message.get("parts") or []
    text_chunks: list[str] = []

    for part in parts:
        kind = part.get("kind") or part.get("type")
        if kind == "data":
            data = part.get("data") or {}
            op = data.get("operation") or data.get("skill")
            if op in KNOWN_OPS:
                return op, dict(data.get("arguments") or {}), ""
        elif kind == "text":
            text_chunks.append(part.get("text") or "")

    raw = "\n".join(text_chunks).strip()
    if not raw:
        return None, {}, ""

    # A JSON object in a text part is a perfectly good invocation.
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            op = obj.get("operation") or obj.get("skill")
            if op in KNOWN_OPS:
                return op, dict(obj.get("arguments") or {}), raw
        except json.JSONDecodeError:
            pass

    # `operation: payload`
    match = _PREFIX_RE.match(raw)
    if match and match.group(1) in KNOWN_OPS:
        op, payload = match.group(1), match.group(2).strip()
        return op, _payload_to_args(op, payload), raw

    bare = raw.strip().lower()
    if bare in KNOWN_OPS:
        return bare, {}, raw

    return None, {}, raw


def _payload_to_args(op: str, payload: str) -> dict[str, Any]:
    if payload.startswith("{"):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            pass
    positional = {
        "list_files": "path",
        "read_file": "path",
        "delete_path": "path",
        "run_code": "code",
        "deploy_site": "name",
        "deployment_logs": "name",
        "delete_deployment": "name",
    }
    key = positional.get(op)
    return {key: payload} if key and payload else {}


def _text_part(text: str) -> dict:
    return {"kind": "text", "text": text}


def _data_part(data: Any) -> dict:
    return {"kind": "data", "data": data}


def _guidance_text() -> str:
    # The example has to be an operation this space actually performs. Teaching
    # `run_code` where nothing executes sends the peer straight into a refusal.
    if execution_enabled():
        example = '{"operation":"run_code","arguments":{"code":"print(1)"}}'
        shorthand = "run_code: print(1)"
    else:
        example = ('{"operation":"write_file","arguments":'
                   '{"path":"public/index.html","content":"<h1>hi</h1>"}}')
        shorthand = "list_files: ."

    lines = [
        "I could not tell which operation you want.",
        "",
        "I am a workspace, not a language model — name the operation explicitly.",
        "The precise form is a DataPart:",
        f'  {{"kind":"data","data":{example}}}',
        f"Text shorthand also works:  {shorthand}",
        "",
        "Available operations:",
    ]
    for cap in available_capabilities():
        if cap.mcp_tool:
            lines.append(f"  {cap.mcp_tool:<20} {cap.description}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# JSON-RPC endpoint
# --------------------------------------------------------------------------
@router.post("/a2a", include_in_schema=False)
async def a2a_endpoint(request: Request, session: SessionDep) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return _json({"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32700, "message": "Parse error"}}, 400)

    req_id = payload.get("id") if isinstance(payload, dict) else None
    method = payload.get("method") if isinstance(payload, dict) else None
    params = (payload.get("params") or {}) if isinstance(payload, dict) else {}

    token = bearer_token(request)
    user = await resolve_api_key(session, token) if token else None
    if user is None:
        return _json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": "Authentication required",
                    "data": {
                        "fix": "Send `Authorization: Bearer ask_...`. The agent card at "
                        f"{get_settings().public_url}/.well-known/agent-card.json "
                        "declares this scheme under securitySchemes.bearerAuth.",
                    },
                },
            },
            401,
        )
    ensure_not_suspended(user)
    await ensure_workspace(session, user)

    if method in ("message/send", "message/stream"):
        return await _message_send(req_id, params, session, user)
    if method == "tasks/get":
        return await _tasks_get(req_id, params, session, user)
    if method == "tasks/cancel":
        return await _tasks_cancel(req_id, params, session, user)

    return _json(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method {method!r} not supported",
                "data": {"supported": ["message/send", "tasks/get", "tasks/cancel"]},
            },
        },
        200,
    )


async def _message_send(
    req_id: Any, params: dict, session: AsyncSession, user: User
) -> Response:
    message = params.get("message") or {}
    context_id = message.get("contextId") or uuid.uuid4().hex
    operation, arguments, raw = parse_message(message)

    task = AgentTask(user_id=user.id, context_id=context_id, state="working")
    session.add(task)
    await session.flush()

    history = [message]
    artifacts: list[dict] = []

    if operation is None:
        task.state = "input-required"
        reply = _agent_message(context_id, task.id, [_text_part(_guidance_text())])
        history.append(reply)
    else:
        try:
            data, guide = await operations.dispatch(session, user, operation, arguments)
            task.state = "completed"
            text = _summarize(operation, data) + render_guide(guide)
            reply = _agent_message(context_id, task.id, [_text_part(text), _data_part(data)])
            history.append(reply)
            artifacts.append(
                {
                    "artifactId": uuid.uuid4().hex,
                    "name": f"{operation}-result",
                    "parts": [_data_part(data)],
                }
            )
        except AgentSpaceError as exc:
            task.state = "failed"
            body = exc.as_dict()["error"]
            reply = _agent_message(
                context_id,
                task.id,
                [
                    _text_part(f"{exc.message}\n\nFIX: {exc.fix}"),
                    _data_part(body),
                ],
            )
            history.append(reply)

    task.history_json = json.dumps(history, default=str)
    task.artifacts_json = json.dumps(artifacts, default=str)
    return _json({"jsonrpc": "2.0", "id": req_id, "result": _task_dict(task)}, 200)


def _agent_message(context_id: str, task_id: str, parts: list[dict]) -> dict:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": uuid.uuid4().hex,
        "contextId": context_id,
        "taskId": task_id,
        "parts": parts,
    }


def _task_dict(task: AgentTask) -> dict:
    return {
        "kind": "task",
        "id": task.id,
        "contextId": task.context_id,
        "status": {
            "state": task.state,
            "timestamp": task.updated_at.isoformat() if task.updated_at else None,
        },
        "history": json.loads(task.history_json or "[]"),
        "artifacts": json.loads(task.artifacts_json or "[]"),
    }


async def _tasks_get(req_id: Any, params: dict, session: AsyncSession, user: User) -> Response:
    task = await session.get(AgentTask, params.get("id", ""))
    if task is None or task.user_id != user.id:
        return _json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": "Task not found",
                    "data": {"fix": "Use the `id` returned by message/send."},
                },
            },
            200,
        )
    return _json({"jsonrpc": "2.0", "id": req_id, "result": _task_dict(task)}, 200)


async def _tasks_cancel(req_id: Any, params: dict, session: AsyncSession, user: User) -> Response:
    task = await session.get(AgentTask, params.get("id", ""))
    if task is None or task.user_id != user.id:
        return _json(
            {"jsonrpc": "2.0", "id": req_id,
             "error": {"code": -32001, "message": "Task not found"}}, 200,
        )
    if task.state in ("completed", "failed", "canceled"):
        return _json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32002,
                    "message": f"Task is already {task.state} and cannot be canceled",
                },
            },
            200,
        )
    task.state = "canceled"
    return _json({"jsonrpc": "2.0", "id": req_id, "result": _task_dict(task)}, 200)


def _json(body: Any, status: int) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False, default=str),
        media_type="application/json",
        status_code=status,
    )
