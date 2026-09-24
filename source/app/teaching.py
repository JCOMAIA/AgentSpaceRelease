"""The self-teaching layer.

The premise of AgentSpace: an agent should never need out-of-band documentation.
Every response carries the map — where you are, what you can do next, and the
exact call to make. Every error carries the repair.

Concretely this module provides:
  * `ok()` / `fail()`   — response envelopes with a `guide` block
  * `AgentSpaceError`   — errors that explain how to fix themselves
  * `onboarding()`      — the first-contact briefing, also served as /llms.txt
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Plan, get_settings


@dataclass
class NextStep:
    """One concrete thing the agent can do right now."""

    do: str
    why: str
    call: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"do": self.do, "why": self.why, "call": self.call}


@dataclass
class Guide:
    you_are_here: str
    next_steps: list[NextStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "you_are_here": self.you_are_here,
            "next_steps": [s.as_dict() for s in self.next_steps],
            "notes": self.notes,
            "full_manual": f"{get_settings().public_url}/llms.txt",
        }


def rest(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Describe a REST call in a shape an agent can execute without guessing."""
    call = {"transport": "rest", "method": method, "path": path}
    call.update(kwargs)
    return call


def ok(data: Any, guide: Guide | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": True, "data": data}
    if guide is not None:
        body["guide"] = guide.as_dict()
    return body


class AgentSpaceError(Exception):
    """An error that teaches. `fix` is mandatory — if we can't say how to
    recover, the error message isn't finished yet."""

    def __init__(
        self,
        code: str,
        message: str,
        fix: str,
        *,
        status_code: int = 400,
        try_this: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.status_code = status_code
        self.try_this = try_this
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "fix": self.fix,
                **({"try_this": self.try_this} if self.try_this else {}),
                **({"details": self.details} if self.details else {}),
            },
            "guide": {"full_manual": f"{get_settings().public_url}/llms.txt"},
        }
        return payload


# --------------------------------------------------------------------------
# Capability catalogue — single source of truth.
# REST docs, MCP tool list and the A2A agent card all render from this.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    description: str
    rest: str
    mcp_tool: str | None
    tags: tuple[str, ...] = ()
    # A space configured to publish but not execute hides these. Promising a
    # capability the instance cannot deliver is worse than not having it: the
    # agent spends its turn discovering the promise was false.
    needs_execution: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "whoami",
        "Inspect your own space",
        "Returns your username, plan, quota usage and public URLs. Safe first call.",
        "GET /api/v1/whoami",
        "whoami",
        ("identity",),
    ),
    Capability(
        "files.list",
        "List files",
        "List entries under a path in your persistent workspace. Path is relative to your root.",
        "GET /api/v1/files?path=.",
        "list_files",
        ("storage",),
    ),
    Capability(
        "files.read",
        "Read a file",
        "Download a file's contents. Text is returned inline; binary as base64.",
        "GET /api/v1/files/raw?path=notes.md",
        "read_file",
        ("storage",),
    ),
    Capability(
        "files.write",
        "Write a file",
        "Create or overwrite a file. Parent directories are created automatically.",
        "PUT /api/v1/files?path=notes.md",
        "write_file",
        ("storage",),
    ),
    Capability(
        "files.upload",
        "Upload a file",
        "Multipart upload for binaries and archives. Zip archives can be auto-extracted.",
        "POST /api/v1/files/upload",
        None,
        ("storage",),
    ),
    Capability(
        "files.delete",
        "Delete a path",
        "Remove a file or a directory tree from your workspace.",
        "DELETE /api/v1/files?path=old/",
        "delete_path",
        ("storage",),
    ),
    Capability(
        "exec.run",
        "Run code in the sandbox",
        "Execute python/node/bash inside an isolated container with your workspace mounted at "
        "/workspace. Returns stdout, stderr and exit code.",
        "POST /api/v1/exec",
        "run_code",
        ("compute",),
        needs_execution=True,
    ),
    Capability(
        "deploy.site",
        "Publish a static site",
        "Serve a workspace directory as a public website at your space URL.",
        "POST /api/v1/deployments (kind=static)",
        "deploy_site",
        ("hosting",),
    ),
    Capability(
        "deploy.service",
        "Publish a running service",
        "Boot a long-lived container running your command (API, app, bot) and reverse-proxy "
        "public traffic to it.",
        "POST /api/v1/deployments (kind=service)",
        "deploy_service",
        ("hosting",),
        needs_execution=True,
    ),
    Capability(
        "deploy.list",
        "List and control deployments",
        "See what you have published, and stop or delete any of it.",
        "GET /api/v1/deployments",
        "list_deployments",
        ("hosting",),
    ),
    Capability(
        "deploy.logs",
        "Read deployment logs",
        "Tail stdout/stderr of a running service to debug it.",
        "GET /api/v1/deployments/{name}/logs",
        "deployment_logs",
        ("hosting", "debug"),
        needs_execution=True,
    ),
)


def execution_enabled() -> bool:
    from .config import get_settings

    return get_settings().sandbox_driver != "none"


def available_capabilities() -> tuple[Capability, ...]:
    """What this instance can actually do.

    Everything that describes the space to an agent renders from this rather
    than from CAPABILITIES: the REST catalogue, the MCP tool list, the A2A
    skills, the manual and the home page. Routing still knows the full set, so
    an agent that asks for `run_code` anyway is taught instead of being told the
    operation does not exist.
    """
    if execution_enabled():
        return CAPABILITIES
    return tuple(c for c in CAPABILITIES if not c.needs_execution)


def capability_table() -> list[dict[str, Any]]:
    return [
        {
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "rest": c.rest,
            "mcp_tool": c.mcp_tool,
            "tags": list(c.tags),
        }
        for c in available_capabilities()
    ]


# Said in every place execution can be reached from, because an agent that
# assumes it has a computer keeps trying until something states otherwise.
# Lives here rather than in the driver so the teaching layer owns the wording
# and the driver imports it, not the other way round.
NO_EXECUTION = (
    "This space publishes files; it does not run code on the server. Build things that "
    "run in the visitor's browser — HTML, CSS and JavaScript — which covers pages, tools, "
    "dashboards, visualisations and games. Write them under `public/` and they are live "
    "at your public URL."
)


# --------------------------------------------------------------------------
# First contact
# --------------------------------------------------------------------------
def onboarding(username: str | None = None, plan: Plan | None = None) -> dict[str, Any]:
    """The briefing an agent gets on its first request.

    Anonymous agents get the version that explains how to obtain a key; agents
    that already authenticated get their own coordinates filled in.
    """
    s = get_settings()
    base = s.public_url
    who = username or "<your-username>"

    mental_model = [
        "You have a private filesystem that survives between sessions. It is your /workspace.",
        f"Anything you write under `public/` is live at {base}/@{who}/ immediately. "
        "There is no deploy step, no build and no publish call.",
    ]
    if execution_enabled():
        mental_model.insert(1, "You can run code against that filesystem in a sandboxed "
                               "container.")
    mental_model.append(
        "Three doors lead to the same space: REST, MCP and A2A. Pick whichever you speak."
    )

    your_space: dict[str, Any] = {
        "workspace_root": "/workspace",
        "public_site_url": f"{base}/@{who}/",
        "the_published_folder": "public/",
    }
    # Only advertised where wildcard DNS actually exists. Behind a quick tunnel
    # `<name>.<host>` does not resolve at all, and an agent handed a URL shape
    # that cannot work will hand it to a person, who finds a dead link.
    if s.subdomain_urls:
        your_space["public_subdomain_url"] = f"https://{who}.{s.base_domain}/"

    doc: dict[str, Any] = {
        "welcome": "You are talking to AgentSpace — a persistent workspace that is also a "
                   "public website.",
        "mental_model": mental_model,
        "how_to_publish": [
            f"1. PUT {base}/api/v1/files?path=public/index.html with your HTML",
            f"2. Open {base}/@{who}/ — it is already live, with no further calls",
            "3. More files under public/ get their own addresses under that URL",
        ],
        "your_space": your_space,
        "doors": {
            "rest": {
                "base_url": f"{base}/api/v1",
                "auth": "Header  Authorization: Bearer <api-key>",
                "start_with": "GET /api/v1/whoami",
            },
            "mcp": {
                "url": f"{base}/mcp",
                "transport": "streamable-http",
                "auth": "Header  Authorization: Bearer <api-key>",
                "note": "Call initialize, then tools/list. Tool descriptions are self-explanatory.",
            },
            "a2a": {
                "agent_card": f"{base}/.well-known/agent-card.json",
                "endpoint": f"{base}/a2a",
                "auth": "Header  Authorization: Bearer <api-key>",
                "note": "JSON-RPC 2.0. Send natural language to message/send.",
            },
        },
        "capabilities": capability_table(),
        "conventions": {
            "publishing": "public/ is the website. Writing a file there publishes it; "
            "deleting it takes it off the web. Everything outside public/ is private.",
            "paths": "All paths are relative to your workspace root. '..' is rejected.",
            "responses": "Every response is {ok, data, guide}. Read `guide.next_steps` "
            "to continue.",
            "errors": "Every error carries `fix` — a literal instruction for recovering.",
        },
    }

    if not execution_enabled():
        doc["important"] = NO_EXECUTION

    if username is None:
        doc["how_to_get_access"] = {
            "step_1": f"A human registers an account at {base}/register",
            "step_2": "They create an API key in the dashboard and hand it to you",
            "step_3": "Send it as `Authorization: Bearer ask_...` on every request",
            "note": "Agents cannot self-register: a human owns the account and its quota.",
        }
    if plan is not None:
        doc["your_plan"] = {
            "name": plan.name,
            "disk_mb": plan.disk_mb,
            "memory_mb": plan.memory_mb,
            "cpus": plan.cpus,
            "max_deployments": plan.max_deployments,
            "exec_timeout_s": plan.exec_timeout_s,
        }
    return doc


def onboarding_markdown() -> str:
    """`/llms.txt` — the whole manual as text, for agents that prefer prose."""
    s = get_settings()
    base = s.public_url
    lines = [
        "# AgentSpace",
        "",
    ]
    if execution_enabled():
        lines += [
            "> A persistent workspace, code sandbox and public hosting for AI agents.",
            "> You get a filesystem, a place to run code, and a URL to publish to.",
        ]
    else:
        lines += [
            "> A persistent workspace that is also a public website.",
            "> Write a file under `public/` and it is live at a URL. Nothing else to do.",
        ]

    # First, because it is the whole product and everything below is detail. A
    # manual that buries the loop gets summarised without it.
    lines += [
        "",
        "## The loop",
        "",
        "    PUT  " + base + "/api/v1/files?path=public/index.html",
        "    open " + base + "/@<your-username>/",
        "",
        "That is all of it. `public/` is the website: writing a file there puts it on the",
        "web immediately, deleting it takes it off. There is no build, no deploy step and",
        "no publish call. Everything outside `public/` is private working space.",
        "",
        "## Authentication",
        "",
        "Every door takes the same credential:",
        "",
        "    Authorization: Bearer ask_xxx",
        "",
        f"A human creates the key at {base}/dashboard. Agents cannot self-register.",
        "",
        "## Three doors, one space",
        "",
        f"- REST — `{base}/api/v1` — start with `GET /api/v1/whoami`",
        f"- MCP  — `{base}/mcp` — streamable HTTP; `initialize` then `tools/list`",
        f"- A2A  — `{base}/.well-known/agent-card.json` — JSON-RPC at `{base}/a2a`",
        "",
        "All three operate on the same files. Write over MCP, read over REST — same bytes.",
        "",
        "## What you can do",
        "",
    ]
    for c in available_capabilities():
        tool = f" (MCP tool `{c.mcp_tool}`)" if c.mcp_tool else ""
        lines.append(f"### {c.title}")
        lines.append(f"{c.description}")
        lines.append(f"`{c.rest}`{tool}")
        lines.append("")

    lines += [
        "## Publishing",
        "",
        "Writing to `public/` is publishing. You do not need anything below this line to",
        "put a page on the web.",
        "",
        f"Your address is `{base}/@<username>/`. A file at `public/about.html` is at",
        f"`{base}/@<username>/about.html`.",
        "",
    ]
    if s.subdomain_urls:
        lines += [
            f"This instance also serves `https://<username>.{s.base_domain}/`.",
            "",
        ]

    if execution_enabled():
        lines += [
            "### Named deployments",
            "",
            "For serving a directory that is not `public/`, or for running a process:",
            "",
            "- `static` — point at a directory, we serve it at `/@<username>/<name>/`.",
            "- `service` — give a command and a port, we run it and proxy traffic to it.",
            "",
        ]
    else:
        lines += [
            "### What this space does not do",
            "",
            NO_EXECUTION,
            "",
            "Do not write a server process and wait for it to start. Nothing will start it,",
            "and there is no deployment kind that would.",
            "",
        ]

    lines += [
        "## Rules of the house",
        "",
        "- Paths are relative to your workspace root. `..` is rejected.",
        "- Hidden files (`.env`, `.git`) are never served, even from `public/`.",
    ]
    if execution_enabled():
        lines.append("- Sandboxes have no internet access unless your plan enables egress.")
    lines += [
        "- Every response is `{ok, data, guide}`. `guide.next_steps` tells you what to do next.",
        "- Every error has a `fix` field. Read it before retrying.",
        "",
    ]
    return "\n".join(lines)
