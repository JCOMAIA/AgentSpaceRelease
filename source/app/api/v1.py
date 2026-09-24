"""REST door: /api/v1.

Thin adapter — parse, call `operations`, wrap in the teaching envelope.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import operations, quotas, storage
from ..db import get_session
from ..deps import current_user, optional_user
from ..models import User
from ..teaching import AgentSpaceError, ok

router = APIRouter(prefix="/api/v1", tags=["agent"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(current_user)]


# --------------------------------------------------------------------------
# Discovery — the only unauthenticated endpoint
# --------------------------------------------------------------------------
@router.get("/hello", summary="First contact: what this place is and how to use it")
async def hello(
    session: SessionDep,
    user: Annotated[User | None, Depends(optional_user)] = None,
) -> dict[str, Any]:
    data, guide = await operations.hello(user, session)
    return ok(data, guide)


@router.get("/whoami", summary="Your identity, quota and public URLs")
async def whoami(session: SessionDep, user: UserDep) -> dict[str, Any]:
    data, guide = await operations.whoami(session, user)
    return ok(data, guide)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
class WriteFileBody(BaseModel):
    content: str = Field(description="File contents. Plain text unless encoding='base64'.")
    encoding: Literal["utf-8", "base64"] = "utf-8"


class MoveBody(BaseModel):
    src: str
    dest: str


@router.get("/files", summary="List a directory in your workspace")
async def list_files(user: UserDep, path: str = Query(".")) -> dict[str, Any]:
    data, guide = await operations.list_files(user, path)
    return ok(data, guide)


@router.get("/files/content", summary="Read a file as JSON (text or base64)")
async def read_file(user: UserDep, path: str = Query(...)) -> dict[str, Any]:
    data, guide = await operations.read_file(user, path)
    return ok(data, guide)


@router.get("/files/raw", summary="Download a file's raw bytes")
async def read_file_raw(user: UserDep, path: str = Query(...)) -> FileResponse:
    target = storage.resolve(user.id, path, must_exist=True)
    if target.is_dir():
        raise AgentSpaceError(
            "is_a_directory",
            f"{path!r} is a directory.",
            "Use GET /api/v1/files?path=... to list it.",
        )
    return FileResponse(target, filename=target.name)


@router.put("/files", summary="Create or overwrite a file")
async def write_file(
    session: SessionDep, user: UserDep, body: WriteFileBody, path: str = Query(...)
) -> dict[str, Any]:
    data, guide = await operations.write_file(session, user, path, body.content, body.encoding)
    return ok(data, guide)


@router.post("/files/upload", summary="Upload a file (multipart); zips can auto-extract")
async def upload_file(
    session: SessionDep,
    user: UserDep,
    file: Annotated[UploadFile, File()],
    path: Annotated[str, Form()] = "",
    extract: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    plan = quotas.plan_of(user)
    limit = plan.max_upload_mb * 1024 * 1024
    raw = await file.read()
    if len(raw) > limit:
        raise AgentSpaceError(
            "file_too_large",
            f"Upload is {len(raw) / 1e6:.1f} MB; the {plan.name} plan allows "
            f"{plan.max_upload_mb} MB.",
            "Compress it, split it, or upgrade the plan.",
            status_code=413,
        )
    await quotas.check_host_disk(len(raw))
    await quotas.check_disk(user, len(raw))

    dest = path or (file.filename or "upload.bin")
    if extract and (file.filename or "").lower().endswith(".zip"):
        tmp = storage.resolve(user.id, f".agentspace/uploads/{file.filename}")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)
        try:
            written = storage.extract_zip(user.id, tmp, path or ".", limit_bytes=limit)
        finally:
            tmp.unlink(missing_ok=True)
        from ..teaching import Guide, NextStep, rest

        return ok(
            {"extracted": written, "count": len(written)},
            Guide(
                you_are_here=f"Extracted {len(written)} files from {file.filename!r}.",
                next_steps=[
                    NextStep("Publish the extracted directory", "It may already be a website.",
                             rest("POST", "/api/v1/deployments",
                                  body={"name": "site", "kind": "static",
                                        "source_dir": path or "."}))
                ],
            ),
        )

    data, guide = await operations.write_file(
        session, user, dest, __import__("base64").b64encode(raw).decode(), "base64"
    )
    return ok(data, guide)


@router.delete("/files", summary="Delete a file or directory tree")
async def delete_file(user: UserDep, path: str = Query(...)) -> dict[str, Any]:
    data, guide = await operations.delete_path(user, path)
    return ok(data, guide)


@router.post("/files/move", summary="Move or rename a path")
async def move_file(user: UserDep, body: MoveBody) -> dict[str, Any]:
    data, guide = await operations.move_path(user, body.src, body.dest)
    return ok(data, guide)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
class ExecBody(BaseModel):
    code: str = Field(description="Source to execute. Your workspace is mounted at /workspace.")
    language: Literal["python", "node", "bash"] = "python"
    timeout_s: int | None = Field(default=None, description="Capped by your plan.")


@router.post("/exec", summary="Run code in an isolated sandbox")
async def exec_code(session: SessionDep, user: UserDep, body: ExecBody) -> dict[str, Any]:
    data, guide = await operations.run_code(
        session, user, body.language, body.code, body.timeout_s
    )
    return ok(data, guide)


# --------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------
class DeployBody(BaseModel):
    name: str = Field(description="Slug used in the public URL, e.g. 'my-site'.")
    kind: Literal["static", "service"] = "static"
    source_dir: str = "."
    command: str | None = Field(default=None, description="Required when kind='service'.")
    port: int | None = Field(default=None, description="Required when kind='service'.")


@router.get("/deployments", summary="List everything you have published")
async def list_deployments(session: SessionDep, user: UserDep) -> dict[str, Any]:
    data, guide = await operations.list_deployments(session, user)
    return ok(data, guide)


@router.post("/deployments", summary="Publish a static site or start a service")
async def create_deployment(
    session: SessionDep, user: UserDep, body: DeployBody
) -> dict[str, Any]:
    if body.kind == "static":
        data, guide = await operations.deploy_site(session, user, body.name, body.source_dir)
    else:
        if body.command is None or body.port is None:
            raise AgentSpaceError(
                "missing_service_fields",
                "A service deployment needs both `command` and `port`.",
                "Example: {\"name\":\"api\",\"kind\":\"service\","
                "\"command\":\"python server.py\",\"port\":8080}",
            )
        data, guide = await operations.deploy_service(
            session, user, body.name, body.command, body.port, body.source_dir
        )
    return ok(data, guide)


@router.get("/deployments/{name}/logs", summary="Tail a service's logs")
async def deployment_logs(
    session: SessionDep, user: UserDep, name: str, tail: int = Query(200, le=2000)
) -> dict[str, Any]:
    data, guide = await operations.deployment_logs(session, user, name, tail)
    return ok(data, guide)


@router.delete("/deployments/{name}", summary="Remove a deployment")
async def delete_deployment(session: SessionDep, user: UserDep, name: str) -> dict[str, Any]:
    data, guide = await operations.delete_deployment(session, user, name)
    return ok(data, guide)
