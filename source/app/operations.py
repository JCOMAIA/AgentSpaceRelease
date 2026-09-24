"""The operations an agent can perform, independent of transport.

REST, MCP and A2A are three thin adapters over this module. A behaviour change
here reaches all three doors at once, which is the only way "same space, three
protocols" stays true as the system grows.

Every operation returns `(data, guide)` — the guide is what teaches the caller
what became possible as a result of what it just did.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import quotas, scheduler, storage
from .config import get_settings
from .models import Deployment, ExecJob, User
from .sandbox import ExecSpec, SandboxUnavailable, ServiceSpec, get_driver
from .teaching import (
    AgentSpaceError,
    Guide,
    NextStep,
    execution_enabled,
    onboarding,
    rest,
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$|^[a-z0-9]$")

# Control bytes that never appear in text but are common in binaries. Tab, LF,
# CR and form feed are excluded because they do appear in text.
_BINARY_CONTROL = bytes(range(0, 9)) + bytes(range(14, 32))


def _decode_for_agent(raw: bytes) -> tuple[str, str]:
    """Return `(content, encoding)`, preferring text.

    Sniffed from the bytes rather than the file extension: agents work with
    plenty of extensionless files (README, Dockerfile, Makefile) and handing
    those back as base64 for no reason makes them unreadable.
    """
    if b"\x00" in raw[:8192] or any(b in _BINARY_CONTROL for b in raw[:8192]):
        return base64.b64encode(raw).decode(), "base64"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode(), "base64"


PUBLIC_DIR = "public"


def public_url_for(user: User, written: str) -> str:
    """The address a visitor uses for a file under `public/`."""
    base = f"{get_settings().public_url.rstrip('/')}/@{user.username}"
    inside = written.removeprefix(PUBLIC_DIR).lstrip("/")
    return f"{base}/" if inside in ("", "index.html") else f"{base}/{inside}"


def space_urls(user: User, deployment: str | None = None) -> dict[str, str]:
    s = get_settings()
    suffix = f"{deployment}/" if deployment else ""
    urls = {
        # The address to hand a person. `/u/...` still resolves and always will,
        # but it is not what anyone should be given to share.
        "path": f"{s.public_url}/@{user.username}/{suffix}",
    }
    # Only where wildcard DNS exists. Otherwise this is a link that resolves
    # nowhere, handed to an agent that will pass it on to a human as if it were
    # real — which is exactly how a working publish turns into a broken share.
    if s.subdomain_urls:
        urls["subdomain"] = f"https://{user.username}.{s.base_domain}/{suffix}"
    if user.custom_domain:
        urls["custom"] = f"https://{user.custom_domain}/{suffix}"
    return urls


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
async def whoami(session: AsyncSession, user: User) -> tuple[dict, Guide]:
    usage = await quotas.usage_summary(session, user)
    usage["sandbox_pool"] = await scheduler.snapshot_state(session, user)
    deployments = (
        await session.scalars(select(Deployment).where(Deployment.user_id == user.id))
    ).all()
    data = {
        "username": user.username,
        "plan": user.plan,
        "workspace_root": "/workspace",
        "urls": space_urls(user),
        "usage": usage,
        "deployments": [d.name for d in deployments],
        "custom_domain": user.custom_domain,
    }
    steps = [
        NextStep(
            "List what is already in your workspace",
            "Your files persist between sessions — you may have left something here.",
            rest("GET", "/api/v1/files?path=."),
        ),
        NextStep(
            f"Put a page on the web at {space_urls(user)['path']}",
            f"Anything under {PUBLIC_DIR}/ is live the moment you write it. "
            "There is no deploy step.",
            rest(
                "PUT",
                f"/api/v1/files?path={PUBLIC_DIR}/index.html",
                body={"content": "<h1>hi</h1>"},
            ),
        ),
    ]
    notes = ["The full manual is at /llms.txt — it is written for agents."]

    # whoami is the recommended first call, so what it suggests is what an agent
    # will try. Offering the sandbox where there is none sends it down a path
    # that ends in a refusal.
    if execution_enabled():
        steps.append(
            NextStep(
                "Run code against those files",
                "The sandbox mounts your workspace at /workspace.",
                rest("POST", "/api/v1/exec", body={"language": "python", "code": "print(2+2)"}),
            )
        )
        notes.insert(
            0,
            f"You have {usage['deployments']['limit'] - usage['deployments']['used']} "
            "deployment slots left.",
        )

    guide = Guide(
        you_are_here=f"Authenticated as {user.username}. This is your private space.",
        next_steps=steps,
        notes=notes,
    )
    return data, guide


async def hello(user: User | None, session: AsyncSession | None = None) -> tuple[dict, Guide]:
    """First contact. Works with or without credentials."""
    plan = quotas.plan_of(user) if user else None
    data = onboarding(user.username if user else None, plan)
    if user is not None:
        data["your_space"]["urls"] = space_urls(user)
        guide = Guide(
            you_are_here=f"You are authenticated as {user.username} and have full access.",
            next_steps=[
                NextStep(
                    "Check your space and quota",
                    "Confirms the key works and shows what room you have.",
                    rest("GET", "/api/v1/whoami"),
                )
            ],
        )
    else:
        guide = Guide(
            you_are_here="You are talking to AgentSpace without credentials.",
            next_steps=[
                NextStep(
                    "Obtain an API key from the human who runs your account",
                    "Everything past this point is per-user and private.",
                    {"transport": "human", "url": f"{get_settings().public_url}/register"},
                )
            ],
            notes=["This endpoint is public. Every other endpoint needs a key."],
        )
    return data, guide


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
async def list_files(user: User, path: str = ".") -> tuple[dict, Guide]:
    entries = storage.list_dir(user.id, path)
    data = {"path": path, "entries": entries, "count": len(entries)}
    steps = [
        NextStep(
            "Read one of these files",
            "Inspect content before changing it.",
            rest("GET", "/api/v1/files/raw?path=<path>"),
        )
    ]
    if not entries:
        steps.insert(
            0,
            NextStep(
                "Create your first file",
                "This directory is empty.",
                rest("PUT", "/api/v1/files?path=index.html", body={"content": "<h1>hi</h1>"}),
            ),
        )
    guide = Guide(
        you_are_here=f"Listing {path!r} in your workspace ({len(entries)} entries).",
        next_steps=steps,
    )
    return data, guide


async def read_file(user: User, path: str) -> tuple[dict, Guide]:
    target = storage.resolve(user.id, path, must_exist=True)
    if target.is_dir():
        raise AgentSpaceError(
            "is_a_directory",
            f"{path!r} is a directory.",
            "Use the file listing operation for directories.",
            try_this=rest("GET", f"/api/v1/files?path={path}"),
        )
    raw = target.read_bytes()
    content, encoding = _decode_for_agent(raw)

    data = {"path": path, "size": len(raw), "encoding": encoding, "content": content}
    guide = Guide(
        you_are_here=f"Read {path!r} ({len(raw)} bytes, {encoding}).",
        next_steps=[
            NextStep(
                "Write a modified version back",
                "Writes overwrite in place.",
                rest("PUT", f"/api/v1/files?path={path}", body={"content": "<new content>"}),
            )
        ],
    )
    return data, guide


async def write_file(
    session: AsyncSession, user: User, path: str, content: str, encoding: str = "utf-8"
) -> tuple[dict, Guide]:
    if encoding == "base64":
        try:
            raw = base64.b64decode(content, validate=True)
        except Exception as exc:
            raise AgentSpaceError(
                "bad_base64",
                f"`content` was not valid base64: {exc}",
                "Either send valid base64, or set encoding='utf-8' and send plain text.",
            ) from exc
    else:
        raw = content.encode("utf-8")

    await quotas.check_host_disk(len(raw))
    await quotas.check_disk(user, len(raw))
    plan = quotas.plan_of(user)
    target = storage.write_file(user.id, path, raw, limit_bytes=plan.max_upload_mb * 1024 * 1024)

    written = storage.relative(user.id, target)
    is_public = written == PUBLIC_DIR or written.startswith(f"{PUBLIC_DIR}/")

    # This guide fires after every single write, which makes it the most-read
    # sentence in the product. It used to tell the agent to run the file and to
    # publish its directory — one impossible where nothing executes, the other
    # unnecessary because public/ is already served. A connected agent duly
    # reported both back to its user as things it had been asked to do.
    data: dict[str, Any] = {"path": written, "size": len(raw), "live": is_public}
    steps: list[NextStep] = []
    notes: list[str] = []

    if is_public:
        url = public_url_for(user, written)
        data["url"] = url
        steps.append(
            NextStep(
                "Open it",
                "It is already live. There is no publish step, and no deployment to create.",
                {"transport": "http", "method": "GET", "url": url},
            )
        )
    else:
        steps.append(
            NextStep(
                f"Move it under {PUBLIC_DIR}/ to put it on the web",
                f"Only {PUBLIC_DIR}/ is served; everything else is private working space.",
                rest(
                    "PUT",
                    f"/api/v1/files?path={PUBLIC_DIR}/{Path(written).name}",
                    body={"content": "<the same content>"},
                ),
            )
        )
        notes.append(
            f"{written!r} is saved but not on the web — it is outside {PUBLIC_DIR}/."
        )

    if execution_enabled():
        steps.append(
            NextStep(
                "Run it",
                "Files only matter once they execute.",
                rest(
                    "POST",
                    "/api/v1/exec",
                    body={"language": "python", "code": f"exec(open('{written}').read())"},
                ),
            )
        )

    guide = Guide(
        you_are_here=f"Wrote {len(raw)} bytes to {written!r}.",
        next_steps=steps,
        notes=notes,
    )
    return data, guide


async def delete_path(user: User, path: str) -> tuple[dict, Guide]:
    storage.delete_path(user.id, path)
    guide = Guide(
        you_are_here=f"Deleted {path!r}.",
        next_steps=[
            NextStep("Confirm the result", "See the directory as it stands now.",
                     rest("GET", "/api/v1/files?path=."))
        ],
    )
    return {"path": path, "deleted": True}, guide


async def move_path(user: User, src: str, dest: str) -> tuple[dict, Guide]:
    target = storage.move_path(user.id, src, dest)
    guide = Guide(you_are_here=f"Moved {src!r} to {dest!r}.", next_steps=[])
    return {"from": src, "to": storage.relative(user.id, target)}, guide


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
async def run_code(
    session: AsyncSession,
    user: User,
    language: str,
    code: str,
    timeout_s: int | None = None,
) -> tuple[dict, Guide]:
    from .sandbox.base import LANGUAGES

    if language not in LANGUAGES:
        raise AgentSpaceError(
            "unsupported_language",
            f"{language!r} is not available in the sandbox.",
            f"Use one of: {', '.join(sorted(LANGUAGES))}.",
            details={"supported": sorted(LANGUAGES)},
        )
    await quotas.check_exec_budget(session, user)
    plan = quotas.plan_of(user)
    timeout = min(timeout_s or plan.exec_timeout_s, plan.exec_timeout_s)

    spec = ExecSpec(
        user_id=user.id,
        workspace_path=storage.workspace_root(user.id),
        language=language,
        code=code,
        timeout_s=timeout,
        memory_mb=plan.memory_mb,
        cpus=plan.cpus,
        allow_egress=False,
    )
    # Admission first: starting the container before knowing the box can hold it
    # is how a burst of concurrent runs takes the machine down.
    reservation = await scheduler.acquire(session, user, plan.memory_mb, timeout)
    try:
        result = await get_driver().run(spec)
    except SandboxUnavailable as exc:
        raise AgentSpaceError(
            "sandbox_unavailable",
            f"The execution backend is not reachable right now: {exc}",
            "This is our problem, not yours. Retry in a few seconds; your files are untouched.",
            status_code=503,
        ) from exc
    finally:
        await scheduler.release(session, reservation.slot_id)

    job = ExecJob(
        user_id=user.id,
        language=language,
        status="timeout" if result.timed_out else "finished",
        exit_code=result.exit_code,
        stdout=result.stdout[:100_000],
        stderr=result.stderr[:100_000],
        duration_ms=result.duration_ms,
    )
    session.add(job)
    await quotas.record_usage(session, user, "exec_seconds", result.duration_ms / 1000)

    data = {
        "job_id": job.id,
        "language": language,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
        "queued_ms": reservation.waited_ms,
    }

    if result.timed_out:
        steps = [
            NextStep(
                "Shorten the work or raise the timeout",
                f"Your plan caps a single run at {plan.exec_timeout_s}s.",
                rest("POST", "/api/v1/exec", body={"language": language, "code": "<smaller job>"}),
            )
        ]
        here = f"Run hit the {timeout}s limit and was killed."
    elif result.exit_code != 0:
        steps = [
            NextStep(
                "Read stderr and retry",
                "The `stderr` field above holds the traceback that says what broke.",
                rest("POST", "/api/v1/exec", body={"language": language, "code": "<fixed code>"}),
            )
        ]
        here = f"Run finished with exit code {result.exit_code}."
    else:
        steps = [
            NextStep(
                "Check what the run left in your workspace",
                "Anything written to /workspace persists.",
                rest("GET", "/api/v1/files?path=."),
            ),
            NextStep(
                "Publish the result",
                "If it produced a site or an app, put it on a URL.",
                rest("POST", "/api/v1/deployments",
                     body={"name": "site", "kind": "static", "source_dir": "."}),
            ),
        ]
        here = f"Run succeeded in {result.duration_ms} ms."

    notes = ["Files written under /workspace survive; everything else in the container is gone."]
    if reservation.queued:
        # Say it plainly: the agent otherwise reads the extra latency as its own
        # code being slow and starts optimising the wrong thing.
        notes.append(
            f"This run waited {reservation.waited_ms} ms for a free sandbox slot before "
            "starting. The box was busy; your code did not cause the delay."
        )
    return data, Guide(you_are_here=here, next_steps=steps, notes=notes)


# --------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------
def _validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise AgentSpaceError(
            "invalid_name",
            f"{name!r} is not a usable deployment name.",
            "Use lowercase letters, digits and hyphens, 1-40 chars, "
            "starting and ending with a letter or digit. Example: 'my-site'.",
        )
    return name


async def _get_deployment(session: AsyncSession, user: User, name: str) -> Deployment:
    dep = await session.scalar(
        select(Deployment).where(Deployment.user_id == user.id, Deployment.name == name)
    )
    if dep is None:
        raise AgentSpaceError(
            "deployment_not_found",
            f"You have no deployment named {name!r}.",
            "List your deployments to see the real names.",
            status_code=404,
            try_this=rest("GET", "/api/v1/deployments"),
        )
    return dep


async def deploy_site(
    session: AsyncSession, user: User, name: str, source_dir: str = "."
) -> tuple[dict, Guide]:
    name = _validate_name(name)
    root = storage.resolve(user.id, source_dir, must_exist=True)
    if not root.is_dir():
        raise AgentSpaceError(
            "not_a_directory",
            f"{source_dir!r} is a file; a static site needs a directory.",
            "Pass the directory that contains your index.html.",
        )

    existing = await session.scalar(
        select(Deployment).where(Deployment.user_id == user.id, Deployment.name == name)
    )
    if existing is None:
        await quotas.check_deployments(session, user)
        existing = Deployment(user_id=user.id, name=name, kind="static")
        session.add(existing)
    existing.kind = "static"
    existing.source_dir = storage.relative(user.id, root)
    existing.status = "live"
    await session.flush()

    urls = space_urls(user, name)
    has_index = (root / "index.html").exists()
    notes = [
        "Everything in this directory becomes public, not just the pages you link to. "
        "Hidden entries (.env, .git, .ssh) are always refused, but a secret in a "
        "normally-named file would be served."
    ]
    if not has_index:
        notes.append(
            "There is no index.html in that directory, so the root URL will show a file listing."
        )
    # Naming the files beats a generic warning: the agent can act on a list.
    risky = storage.sensitive_entries(root)
    if risky:
        notes.append(
            "These look like credentials and WILL be served publicly: "
            + ", ".join(risky)
            + ". Move them outside the published directory, or rename them with a leading dot."
        )
    data = {"name": name, "kind": "static", "source_dir": existing.source_dir,
            "status": "live", "urls": urls}
    return data, Guide(
        you_are_here=f"Static site {name!r} is live at {urls['path']}",
        next_steps=[
            NextStep("Open the URL to verify it", "Confirm what visitors will see.",
                     {"transport": "http", "method": "GET", "url": urls["path"]}),
            NextStep("Update it by writing files", "Changes are served immediately, no redeploy.",
                     rest("PUT", f"/api/v1/files?path={existing.source_dir}/index.html",
                          body={"content": "<h1>updated</h1>"})),
        ],
        notes=notes,
    )


async def deploy_service(
    session: AsyncSession,
    user: User,
    name: str,
    command: str,
    port: int,
    source_dir: str = ".",
) -> tuple[dict, Guide]:
    name = _validate_name(name)
    if not command.strip():
        raise AgentSpaceError(
            "missing_command",
            "A service needs a command to run.",
            "Example: command='python server.py', port=8080. "
            "Your process must listen on 0.0.0.0 at that port.",
        )
    if not (1024 <= port <= 65535):
        raise AgentSpaceError(
            "invalid_port",
            f"Port {port} is out of range.",
            "Pick a port between 1024 and 65535 — 8080 is a good default.",
        )
    root = storage.resolve(user.id, source_dir, must_exist=True)

    existing = await session.scalar(
        select(Deployment).where(Deployment.user_id == user.id, Deployment.name == name)
    )
    if existing is None:
        await quotas.check_deployments(session, user)
        existing = Deployment(user_id=user.id, name=name, kind="service")
        session.add(existing)

    plan = quotas.plan_of(user)
    driver = get_driver()
    if existing.container_id:
        await driver.remove_service(existing.container_id)

    spec = ServiceSpec(
        user_id=user.id,
        name=name,
        workspace_path=storage.workspace_root(user.id),
        source_dir=storage.relative(user.id, root),
        command=command,
        port=port,
        memory_mb=plan.memory_mb,
        cpus=plan.cpus,
        allow_egress=False,
    )
    try:
        handle = await driver.start_service(spec)
    except NotImplementedError as exc:
        raise AgentSpaceError(
            "services_unavailable",
            str(exc),
            "This server is running the development sandbox driver. "
            "Static sites still work; ask the operator to enable Docker for services.",
            status_code=501,
        ) from exc
    except SandboxUnavailable as exc:
        raise AgentSpaceError(
            "sandbox_unavailable",
            f"Could not start the container: {exc}",
            "This is a server-side problem, not something wrong with your code or files. "
            "Retry shortly; nothing in your workspace changed. If it keeps failing, "
            "publishing the directory as a static site works without a container.",
            status_code=503,
            try_this=rest(
                "POST",
                "/api/v1/deployments",
                body={"name": name, "kind": "static", "source_dir": source_dir},
            ),
        ) from exc

    existing.kind = "service"
    existing.source_dir = spec.source_dir
    existing.command = command
    existing.port = port
    existing.container_id = handle.container_id
    existing.internal_host = handle.internal_host
    existing.internal_port = handle.port
    existing.status = "running"
    await session.flush()

    urls = space_urls(user, name)
    data = {"name": name, "kind": "service", "command": command, "port": port,
            "status": "running", "urls": urls}
    return data, Guide(
        you_are_here=f"Service {name!r} is running and proxied at {urls['path']}",
        next_steps=[
            NextStep("Read the logs", "Confirm it actually bound the port instead of crashing.",
                     rest("GET", f"/api/v1/deployments/{name}/logs")),
            NextStep("Call your own service", "End-to-end check through the public URL.",
                     {"transport": "http", "method": "GET", "url": urls["path"]}),
        ],
        notes=[
            f"Your process must listen on 0.0.0.0:{port} — localhost-only binds are unreachable.",
            "The container has no internet access; outbound calls will fail by design.",
            f"After {get_settings().service_idle_minutes} minutes without traffic the container "
            "is stopped to free memory. The next request restarts it automatically, so the URL "
            "keeps working — that first request is just slower. Do not rely on in-memory state "
            "surviving; write anything you need to keep to your workspace.",
        ],
    )


async def list_deployments(session: AsyncSession, user: User) -> tuple[dict, Guide]:
    deps = (await session.scalars(select(Deployment).where(Deployment.user_id == user.id))).all()
    items = [
        {
            "name": d.name,
            "kind": d.kind,
            "status": d.status,
            "source_dir": d.source_dir,
            "command": d.command,
            "port": d.port,
            "last_request_at": d.last_request_at.isoformat() if d.last_request_at else None,
            "urls": space_urls(user, d.name),
        }
        for d in deps
    ]
    if any(d.status == "idle" for d in deps):
        # "idle" reads like a failure unless we say otherwise.
        items_note = (
            "A service marked `idle` was stopped after a period without traffic. Its URL still "
            "works — the next request restarts it."
        )
    else:
        items_note = None
    plan = quotas.plan_of(user)
    steps = []
    if not items:
        steps.append(
            NextStep("Publish something", "You have nothing live yet.",
                     rest("POST", "/api/v1/deployments",
                          body={"name": "site", "kind": "static", "source_dir": "."}))
        )
    return {"deployments": items, "limit": plan.max_deployments}, Guide(
        you_are_here=f"You have {len(items)} of {plan.max_deployments} deployment slots in use.",
        next_steps=steps,
        notes=[items_note] if items_note else [],
    )


async def delete_deployment(session: AsyncSession, user: User, name: str) -> tuple[dict, Guide]:
    dep = await _get_deployment(session, user, name)
    if dep.container_id:
        await get_driver().remove_service(dep.container_id)
    await session.delete(dep)
    return {"name": name, "deleted": True}, Guide(
        you_are_here=f"Deployment {name!r} removed. Its files are still in your workspace.",
        next_steps=[NextStep("See what is still live", "", rest("GET", "/api/v1/deployments"))],
    )


async def deployment_logs(
    session: AsyncSession, user: User, name: str, tail: int = 200
) -> tuple[dict, Guide]:
    dep = await _get_deployment(session, user, name)
    if dep.kind != "service" or not dep.container_id:
        raise AgentSpaceError(
            "no_logs",
            f"{name!r} is a static site — static sites have no process and no logs.",
            "Static files are served directly; open the URL to check them.",
            try_this={"transport": "http", "method": "GET",
                      "url": space_urls(user, name)["path"]},
        )
    text = await get_driver().logs(dep.container_id, tail=tail)
    return {"name": name, "logs": text, "tail": tail}, Guide(
        you_are_here=f"Last {tail} log lines for {name!r}.",
        next_steps=[
            NextStep("Fix the code and redeploy", "Redeploying replaces the container in place.",
                     rest("POST", "/api/v1/deployments",
                          body={"name": name, "kind": "service",
                                "command": dep.command, "port": dep.port}))
        ],
    )


# --------------------------------------------------------------------------
# Dispatch table — MCP and A2A both route through this.
# --------------------------------------------------------------------------
async def dispatch(
    session: AsyncSession, user: User, operation: str, args: dict[str, Any]
) -> tuple[Any, Guide]:
    match operation:
        case "whoami":
            return await whoami(session, user)
        case "list_files":
            return await list_files(user, args.get("path", "."))
        case "read_file":
            return await read_file(user, _require(args, "path", operation))
        case "write_file":
            return await write_file(
                session, user, _require(args, "path", operation),
                _require(args, "content", operation), args.get("encoding", "utf-8"),
            )
        case "delete_path":
            return await delete_path(user, _require(args, "path", operation))
        case "move_path":
            return await move_path(
                user, _require(args, "from", operation), _require(args, "to", operation)
            )
        case "run_code":
            return await run_code(
                session, user, args.get("language", "python"),
                _require(args, "code", operation), args.get("timeout_s"),
            )
        case "deploy_site":
            return await deploy_site(
                session, user, _require(args, "name", operation), args.get("source_dir", ".")
            )
        case "deploy_service":
            return await deploy_service(
                session, user, _require(args, "name", operation),
                _require(args, "command", operation), int(_require(args, "port", operation)),
                args.get("source_dir", "."),
            )
        case "list_deployments":
            return await list_deployments(session, user)
        case "delete_deployment":
            return await delete_deployment(session, user, _require(args, "name", operation))
        case "deployment_logs":
            return await deployment_logs(
                session, user, _require(args, "name", operation), int(args.get("tail", 200))
            )
        case _:
            raise AgentSpaceError(
                "unknown_operation",
                f"There is no operation called {operation!r}.",
                "Call `tools/list` over MCP, or GET /api/v1/hello, for the current catalogue.",
                status_code=404,
            )


def _require(args: dict[str, Any], key: str, operation: str) -> Any:
    if key not in args or args[key] is None:
        raise AgentSpaceError(
            "missing_argument",
            f"Operation {operation!r} requires the argument {key!r}.",
            f"Add {key!r} to your arguments and call again.",
            details={"missing": key, "provided": sorted(args)},
        )
    return args[key]
