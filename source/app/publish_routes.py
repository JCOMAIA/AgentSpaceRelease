"""The two ways in that need no authenticated HTTP client.

    GET  /publish            the box you paste a chatbot's answer into
    POST /publish            extract, write, redirect to the live page
    GET  /publish/token      mint a draft token and the block to paste into a chat

    GET  /edit?path=...      the current page in a box, to change or repaste into
    POST /edit               save it back to the same filename, so the shared
                             link keeps pointing at the thing people were sent

    GET  /d/new              a chatbot stages a draft   (token in the query)
    GET  /d/{id}/add         ...and appends to it, for pages too big for one URL
    GET  /d/{id}             the human sees it and decides
    POST /d/{id}/publish     the human publishes it     (session cookie)

Everything under /d/ that a chatbot can reach is inert. Publishing needs a
signed-in person, because a GET that published on its own would fire from chat
link previews and browser prefetch — and the whole point of this space is that
its links get pasted into chats.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from . import operations, publish, quotas, storage
from .config import get_settings
from .db import get_session
from .deps import optional_user
from .models import User
from .teaching import AgentSpaceError
from .templating import build_templates
from .web import BASE_DIR, _ctx

router = APIRouter(tags=["publish"], include_in_schema=False)
templates = build_templates(BASE_DIR / "templates")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
MaybeUser = Annotated[User | None, Depends(optional_user)]

PUBLIC_DIR = "public"


def _live_url(user: User, filename: str) -> str:
    base = f"{get_settings().public_url.rstrip('/')}/@{user.username}"
    return base + "/" if filename == "index.html" else f"{base}/{filename}"


async def _write_page(user: User, filename: str, content: str) -> str:
    raw = content.encode("utf-8")
    await quotas.check_host_disk(len(raw))
    await quotas.check_disk(user, len(raw))
    plan = quotas.plan_of(user)
    storage.write_file(
        user.id, f"{PUBLIC_DIR}/{filename}", raw,
        limit_bytes=plan.max_upload_mb * 1024 * 1024,
    )
    return _live_url(user, filename)


# --------------------------------------------------------------------------
# Paste
# --------------------------------------------------------------------------
@router.get("/publish", response_class=HTMLResponse)
async def publish_page(request: Request, user: MaybeUser = None):
    if not user:
        return RedirectResponse("/login?next=/publish", status_code=302)
    return templates.TemplateResponse(
        request,
        "publish.html",
        _ctx(request, user, drafts=publish.list_drafts(user.id),
             pages=operations.space_urls(user)),
    )


@router.post("/publish", response_class=HTMLResponse)
async def publish_paste(
    request: Request,
    user: MaybeUser = None,
    pasted: str = Form(""),
    name: str = Form(""),
):
    if not user:
        return RedirectResponse("/login?next=/publish", status_code=302)

    extracted = publish.extract_html(pasted)
    filename = publish.normalise_name(name or extracted.name, extracted.content)
    existed = _page_path(user, filename).is_file()
    url = await _write_page(user, filename, extracted.content)

    return _published(request, user, url, filename, extracted.note, replaced=existed)


def _page_path(user: User, filename: str):
    return storage.resolve(user.id, f"{PUBLIC_DIR}/{filename}")


def _published(request, user, url, filename, note, *, replaced: bool):
    """The page you land on after publishing.

    It says whether something was replaced, because pasting a revision whose
    title drifted quietly creates a second page while the link already shared
    points at the first — and the only moment that is cheap to notice is now.
    """
    return templates.TemplateResponse(
        request,
        "published.html",
        _ctx(request, user, url=url, filename=filename, note=note, replaced=replaced,
             edit_url=f"/edit?path={PUBLIC_DIR}/{filename}",
             others=[p for p in _published_pages(user) if p["filename"] != filename],
             profile=f"{get_settings().public_url.rstrip('/')}/@{user.username}/"),
    )


def _published_pages(user: User) -> list[dict]:
    public = storage.workspace_root(user.id) / PUBLIC_DIR
    if not public.is_dir():
        return []
    pages = []
    for path in sorted(public.glob("*.htm*")):
        if path.name.startswith("."):
            continue
        pages.append({"filename": path.name, "url": _live_url(user, path.name)})
    return pages


# --------------------------------------------------------------------------
# Editing what is already there
# --------------------------------------------------------------------------
@router.get("/edit", response_class=HTMLResponse)
async def edit_page(request: Request, path: str = Query(""), user: MaybeUser = None):
    if not user:
        return RedirectResponse(f"/login?next=/edit?path={path}", status_code=302)

    target = storage.resolve(user.id, path, must_exist=True)
    if target.is_dir():
        raise AgentSpaceError(
            "is_a_directory", f"{path!r} is a folder.",
            "Pick a file — your pages are listed on your profile.",
        )
    filename = target.name
    return templates.TemplateResponse(
        request,
        "edit.html",
        _ctx(request, user, path=path, filename=filename,
             content=target.read_text(encoding="utf-8", errors="replace"),
             url=_live_url(user, filename) if path.startswith(f"{PUBLIC_DIR}/") else None),
    )


@router.post("/edit", response_class=HTMLResponse)
async def edit_save(
    request: Request,
    user: MaybeUser = None,
    path: str = Form(""),
    content: str = Form(""),
):
    if not user:
        return RedirectResponse("/login?next=/publish", status_code=302)

    # The path comes from the form, so it is resolved the same way every other
    # caller-supplied path is. The filename is not re-derived from the title:
    # keeping it fixed is the entire point of editing rather than re-publishing.
    target = storage.resolve(user.id, path, must_exist=True)
    extracted = publish.extract_html(content)
    filename = target.name
    url = await _write_page(user, filename, extracted.content)
    return _published(request, user, url, filename, extracted.note, replaced=True)


@router.post("/edit/delete")
async def edit_delete(user: MaybeUser = None, path: str = Form("")) -> Any:
    if not user:
        return RedirectResponse("/login", status_code=302)
    storage.delete_path(user.id, path)
    return RedirectResponse(f"/@{user.username}/", status_code=303)


@router.get("/publish/token")
async def publish_token(user: MaybeUser = None) -> JSONResponse:
    """Mint a token and the exact words to paste into a chat."""
    if not user:
        raise AgentSpaceError(
            "unauthenticated", "Sign in first.",
            "Open /login, then come back to /publish.", status_code=401,
        )
    token = publish.mint_draft_token(user.id)
    base = get_settings().public_url.rstrip("/")
    return JSONResponse({
        "ok": True,
        "data": {
            "token": token,
            "expires_in_hours": publish.DRAFT_TTL_S // 3600,
            "instructions": (
                "Você pode publicar uma página web para mim. Faça uma requisição GET para:\n"
                f"{base}/d/new?t={token}&name=<nome-do-arquivo>&c=<html-url-encoded>\n\n"
                "Se a página for grande demais para uma URL, envie o começo em /d/new e "
                "continue com:\n"
                f"{base}/d/<id>/add?t={token}&c=<mais-html-url-encoded>\n\n"
                "A resposta traz um link de confirmação. Me mande esse link — eu confirmo "
                "e a página entra no ar."
            ),
        },
    })


# --------------------------------------------------------------------------
# Draft, staged by whatever can only do GET
# --------------------------------------------------------------------------
def _draft_reply(base: str, user_id: str, draft_id: str, size: int) -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "data": {
            "draft_id": draft_id,
            "bytes": size,
            "confirm_url": f"{base}/d/{draft_id}",
            "append_url": f"{base}/d/{draft_id}/add?t=<token>&c=<more-html>",
        },
        "guide": {
            "you_are_here": f"Draft {draft_id} holds {size} bytes. It is NOT published.",
            "next_steps": [
                {
                    "do": "Give the confirm_url to the person you are helping",
                    "why": "Only a signed-in human can publish. That is deliberate.",
                    "call": {"transport": "human", "action": "send them confirm_url"},
                },
                {
                    "do": "Append the rest first, if the page was cut short",
                    "why": "A URL cannot carry a whole page; several can.",
                    "call": {"transport": "rest", "method": "GET",
                             "path": f"/d/{draft_id}/add?t=<token>&c=<more-html>"},
                },
            ],
        },
    })


@router.get("/d/new")
async def draft_new(
    request: Request,
    t: str = Query("", description="draft token from /publish"),
    c: str = Query("", description="HTML, url-encoded"),
    name: str = Query(""),
) -> JSONResponse:
    user_id = publish.require_draft_token(t)
    if not c.strip():
        raise AgentSpaceError(
            "empty_draft", "No content arrived in `c`.",
            "Put the page HTML in the `c` query parameter, url-encoded.",
        )
    draft_id = publish.create_draft(user_id, name, c)
    base = get_settings().public_url.rstrip("/")
    return _draft_reply(base, user_id, draft_id, len(c.encode()))


@router.get("/d/{draft_id}/add")
async def draft_add(
    draft_id: str,
    t: str = Query(""),
    c: str = Query(""),
) -> JSONResponse:
    user_id = publish.require_draft_token(t)
    size = publish.append_draft(user_id, draft_id, c)
    base = get_settings().public_url.rstrip("/")
    return _draft_reply(base, user_id, draft_id, size)


@router.get("/d/{draft_id}", response_class=HTMLResponse)
async def draft_review(request: Request, draft_id: str, user: MaybeUser = None):
    if not user:
        return RedirectResponse(f"/login?next=/d/{draft_id}", status_code=302)
    draft = publish.load_draft(user.id, draft_id)
    content = draft.get("content", "")
    filename = publish.normalise_name(draft.get("name"), content)
    return templates.TemplateResponse(
        request,
        "draft.html",
        _ctx(request, user, draft_id=draft_id, filename=filename, content=content,
             bytes=len(content.encode()),
             would_be=_live_url(user, filename)),
    )


@router.post("/d/{draft_id}/publish", response_class=HTMLResponse)
async def draft_publish(
    request: Request,
    draft_id: str,
    user: MaybeUser = None,
    name: str = Form(""),
):
    if not user:
        return RedirectResponse(f"/login?next=/d/{draft_id}", status_code=302)
    draft = publish.load_draft(user.id, draft_id)
    content = draft.get("content", "")
    filename = publish.normalise_name(name or draft.get("name"), content)
    existed = _page_path(user, filename).is_file()
    url = await _write_page(user, filename, content)
    publish.discard_draft(user.id, draft_id)
    return _published(request, user, url, filename, "", replaced=existed)


@router.post("/d/{draft_id}/discard")
async def draft_discard(draft_id: str, user: MaybeUser = None) -> Any:
    if not user:
        return RedirectResponse("/login", status_code=302)
    publish.discard_draft(user.id, draft_id)
    return RedirectResponse("/publish", status_code=303)
