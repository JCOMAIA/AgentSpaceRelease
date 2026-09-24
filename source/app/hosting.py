"""Public hosting: static files and reverse-proxied services.

Three ways to reach the same space, resolved to one canonical internal route
`/u/{username}/{rest}`:

    agentspace.dev/u/alice/site/     path mode      (always available)
    alice.agentspace.dev/site/       subdomain mode (needs wildcard DNS)
    alice-owns-this.com/site/        custom domain  (CNAME + on-demand TLS)

The first path segment names a deployment. If it names no deployment, the
request falls through to the one flagged default, so `/u/alice/` can be a real
homepage instead of a directory index.
"""

from __future__ import annotations

import hashlib
import html
import mimetypes
from email.utils import formatdate
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import profile as profiles
from . import reaper, storage
from .config import get_settings
from .db import get_session
from .deps import optional_user
from .models import Deployment, User
from .teaching import execution_enabled
from .templating import build_templates

router = APIRouter(tags=["hosting"], include_in_schema=False)
templates = build_templates(Path(__file__).parent / "templates")
SessionDep = Annotated[AsyncSession, Depends(get_session)]
MaybeUser = Annotated[User | None, Depends(optional_user)]

# Published files run on the same origin as the dashboard, so without this a page
# someone shares in a chat can act as whoever opens it: the browser attaches the
# visitor's session cookie to `fetch('/api/v1/account/keys')` on its own, and
# httponly does not help because the script never touches the cookie. `sandbox`
# without `allow-same-origin` puts user content in an opaque origin, where those
# requests carry no credentials and localStorage is unreachable.
#
# The real fix is a separate content domain; this is the one-header version that
# holds until there is one. It costs published pages persistent client-side
# storage, which is the trade being made deliberately.
USER_CONTENT_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox "
    "allow-modals allow-downloads"
)

# Without an explicit policy, caches fall back to heuristic freshness — roughly a
# tenth of the age of the document — and serve a stored copy without asking. For
# a space whose pages are edited and then re-read, that is fatal in a specific
# way: an agent told to look at its own page again gets the version from before
# the edit and reasons from it. It happened in testing, and the agent reported
# an empty space that had been full for hours.
#
# `no-cache` does not mean "do not store". It means "store, but revalidate every
# time", so the ETag still turns a repeat visit into a 304 with no body.
USER_CONTENT_CACHE = "no-cache, must-revalidate"


def _user_content_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": USER_CONTENT_CSP,
        "Cache-Control": USER_CONTENT_CACHE,
    }
    headers.update(extra or {})
    return headers


def _validators(target: Path) -> dict[str, str]:
    """ETag and Last-Modified, computed here rather than left to the response.

    Starlette's FileResponse sets these but never acts on `If-None-Match`, so
    every revalidation was answering 200 with the whole body — which turns
    `no-cache` from "cheap check" into "send it all again, every time".
    Computing them here means the same values can be compared on the way in.
    """
    stat = target.stat()
    tag = hashlib.md5(  # noqa: S324 - a cache validator, not a security boundary
        f"{stat.st_mtime_ns}-{stat.st_size}".encode()
    ).hexdigest()
    return {
        "ETag": f'"{tag}"',
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    }


def _unchanged(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    # A client may send several, and a proxy may have weakened them.
    return any(
        candidate.strip().removeprefix("W/") == etag
        for candidate in if_none_match.split(",")
    )

# Headers that describe a specific hop and must not be forwarded verbatim.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# The directory that is the website, with no deploy call. Writing a file here is
# the entire publish step, which is what lets an agent hand over a working link
# in the same turn it finished the page.
PUBLIC_DIR = "public"

# Explicit, because `mimetypes.guess_type` reads the host's mime registry and
# what it answers varies by machine — on Windows `.js` is commonly text/plain,
# which browsers refuse for modules and under `nosniff`. What we serve must not
# depend on the box we happen to be running on.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".pdf": "application/pdf",
}


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in CONTENT_TYPES:
        return CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"

_proxy_client: httpx.AsyncClient | None = None


def proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=200),
        )
    return _proxy_client


async def close_proxy_client() -> None:
    global _proxy_client
    if _proxy_client is not None:
        await _proxy_client.aclose()
        _proxy_client = None


async def resolve_host(session: AsyncSession, host: str) -> str | None:
    """Map a Host header to a username, or None if it is the apex domain."""
    hostname = host.split(":", 1)[0].lower().rstrip(".")
    base = get_settings().base_domain.split(":", 1)[0].lower()

    if hostname in (base, f"www.{base}", "localhost", "127.0.0.1"):
        return None
    if hostname.endswith(f".{base}"):
        label = hostname[: -len(base) - 1]
        return label if "." not in label else None

    user = await session.scalar(select(User).where(User.custom_domain == hostname))
    return user.username if user else None


def _not_found(message: str, detail: str) -> HTMLResponse:
    body = f"""<!doctype html><meta charset="utf-8">
<title>404 — AgentSpace</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:34rem;margin:18vh auto;padding:0 1.5rem;
color:#111}}code{{background:#f3f3f3;padding:.15em .4em;border-radius:4px}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}
code{{background:#222}}}}</style>
<h1>404</h1><p>{html.escape(message)}</p><p style="color:#888">{detail}</p>
<p><a href="{html.escape(get_settings().public_url)}">AgentSpace</a></p>"""
    return HTMLResponse(body, status_code=404)


def _directory_index(rel_url: str, entries: list[Path]) -> HTMLResponse:
    rows = "\n".join(
        f'<li><a href="{html.escape(rel_url.rstrip("/"))}/{html.escape(e.name)}'
        f'{"/" if e.is_dir() else ""}">{html.escape(e.name)}'
        f'{"/" if e.is_dir() else ""}</a></li>'
        for e in sorted(entries, key=lambda p: (p.is_file(), p.name.lower()))
    )
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>Index of {html.escape(rel_url)}</title>
<style>body{{font:15px/1.7 ui-monospace,monospace;max-width:44rem;margin:6vh auto;padding:0 1.5rem}}
h1{{font-size:1.1rem}}li{{list-style:none}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}a{{color:#7ab8ff}}}}</style>
<h1>Index of {html.escape(rel_url)}</h1><ul>{rows}</ul>
<p style="color:#888;font-size:.85em">Add an <code>index.html</code> here to replace this listing.
</p>"""
    )


async def _pick_deployment(
    session: AsyncSession, user: User, segments: list[str]
) -> tuple[Deployment | None, list[str]]:
    """Split the path into (deployment, remaining segments)."""
    deployments = (
        await session.scalars(select(Deployment).where(Deployment.user_id == user.id))
    ).all()
    if not deployments:
        return None, segments

    by_name = {d.name: d for d in deployments}
    if segments and segments[0] in by_name:
        return by_name[segments[0]], segments[1:]

    default = next((d for d in deployments if d.is_default), None)
    if default is None and len(deployments) == 1:
        default = deployments[0]
    if default is None:
        default = by_name.get("site") or by_name.get("www")
    return default, segments


async def serve_space(
    request: Request, session: AsyncSession, username: str, rest: str,
    viewer: User | None = None,
) -> Response:
    user = await session.scalar(select(User).where(User.username == username.lower()))
    if user is None or not user.is_active:
        return _not_found(
            f"There is no space called “{username}” here.",
            "Usernames are case-insensitive and appear in the URL exactly as registered.",
        )

    segments = [s for s in rest.split("/") if s]
    deployment, remainder = await _pick_deployment(session, user, segments)

    if deployment is None:
        # Nothing was explicitly deployed, so `public/` is the site. There is no
        # publish call to forget: the agent writes a file and the link works,
        # which is the difference between handing someone a URL and handing them
        # a set of instructions.
        base = storage.workspace_root(user.id) / PUBLIC_DIR

        # The root of a space is a profile, not a directory listing. Whoever
        # opened this followed a link from someone they know and has never heard
        # of this site; a column of filenames reads as an open file server.
        if not segments and not (base / "index.html").is_file():
            return _profile_page(request, user, base, viewer)

        if not base.is_dir():
            return _nothing_here(username)
        return _serve_static(base, segments, request.url.path, empty_owner=username,
                             if_none_match=request.headers.get("if-none-match"))

    if deployment.kind == "service":
        return await _proxy_to_service(request, session, deployment, remainder)

    root = storage.workspace_root(user.id)
    base = (root / deployment.source_dir).resolve() if deployment.source_dir != "." else root
    return _serve_static(base, remainder, request.url.path,
                         if_none_match=request.headers.get("if-none-match"))


def space_url(username: str) -> str:
    """The canonical public address of a space."""
    return f"{get_settings().public_url.rstrip('/')}/@{username}"


def _profile_page(
    request: Request, user: User, public: Path, viewer: User | None = None
) -> HTMLResponse:
    workspace = storage.workspace_root(user.id)
    profile = profiles.load(workspace, user.username)
    featured, rest = profiles.gallery(public, space_url(user.username))
    # The owner arrives here through the same link they sent everyone else, so
    # this is where they will look for a way to change something.
    is_owner = viewer is not None and viewer.id == user.id
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "request": request,
            "profile": profile,
            "featured": featured,
            "rest": rest,
            "base_url": space_url(user.username),
            "settings": get_settings(),
            "user": viewer if is_owner else None,
            "is_owner": is_owner,
            "single_user": get_settings().single_user_mode,
            "execution": execution_enabled(),
        },
        # The profile lists what is published, so it goes stale the moment
        # anything is added or removed.
        headers={"Cache-Control": USER_CONTENT_CACHE},
    )


def _nothing_here(username: str) -> HTMLResponse:
    return _not_found(
        f"{username} has not put anything here yet.",
        "Files written to public/ in this workspace appear at this address immediately.",
    )


def _serve_static(
    base: Path, segments: list[str], url_path: str, empty_owner: str | None = None,
    if_none_match: str | None = None,
) -> Response:
    # Hidden entries are never public. Publishing a directory publishes what is
    # inside it, and what is inside it is very often `.env`, `.git/config` or a
    # stray SSH key. Refused before touching the filesystem so the answer does
    # not depend on whether the file happens to exist.
    if any(segment.startswith(".") for segment in segments):
        return _not_found(
            "Hidden files are never published.",
            "Paths with a dot-prefixed segment — .env, .git, .ssh — are always refused here, "
            "whether or not they exist.",
        )

    target = (base / "/".join(segments)).resolve() if segments else base

    # The published directory is the boundary; nothing above it is reachable.
    if base != target and base not in target.parents:
        return _not_found("That path is outside the published directory.", "")

    if target.is_dir():
        index = target / "index.html"
        if index.is_file():
            return _file(index, CONTENT_TYPES[".html"], if_none_match)
        if not target.exists():
            return _not_found("No such path in this site.", "")
        visible = [entry for entry in target.iterdir() if not entry.name.startswith(".")]
        if not visible and empty_owner is not None:
            return _nothing_here(empty_owner)
        return _directory_index(url_path, visible)

    if not target.is_file():
        # SPA fallback, but only for paths that look like client routes. `/about`
        # is a route; `/gone.html` names a file, and answering that with the front
        # page and a 200 would make a deleted page look alive to everyone holding
        # the old link.
        spa = base / "index.html"
        if spa.is_file() and not Path(segments[-1] if segments else "").suffix:
            return _file(spa, CONTENT_TYPES[".html"], if_none_match)
        return _not_found(
            f"“{'/'.join(segments)}” is not on this page.",
            "Nothing has been published at that address.",
        )

    # nosniff as well: published files are fetched by the pages beside them,
    # and it closes a content-type confusion class for nothing.
    return _file(target, content_type_for(target), if_none_match,
                 {"X-Content-Type-Options": "nosniff"})


def _file(
    target: Path, media_type: str, if_none_match: str | None,
    extra: dict[str, str] | None = None,
) -> Response:
    validators = _validators(target)
    headers = _user_content_headers({**validators, **(extra or {})})
    if _unchanged(if_none_match, validators["ETag"]):
        # Nothing changed, so say so in a few bytes instead of resending the file.
        return Response(status_code=304, headers=headers)
    return FileResponse(target, media_type=media_type, headers=headers)


async def _proxy_to_service(
    request: Request, session: AsyncSession, deployment: Deployment, segments: list[str]
) -> Response:
    if deployment.status == "idle":
        # Reaped for inactivity. Restart it and serve the request that woke it,
        # so idling costs latency rather than availability.
        if not await reaper.wake(session, deployment):
            return _not_found(
                f"The service “{deployment.name}” could not be restarted.",
                "Its owner can redeploy it with POST /api/v1/deployments.",
            )

    if deployment.status != "running" or not deployment.internal_host:
        return _not_found(
            f"The service “{deployment.name}” is not running.",
            "Its owner can check GET /api/v1/deployments/"
            f"{deployment.name}/logs.",
        )

    await reaper.touch(session, deployment)

    upstream_port = deployment.internal_port or deployment.port
    upstream = f"http://{deployment.internal_host}:{upstream_port}/{'/'.join(segments)}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    # Let the app behind us know how it is really being reached.
    headers["x-forwarded-host"] = request.headers.get("host", "")
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-agentspace-deployment"] = deployment.name

    try:
        upstream_request = proxy_client().build_request(
            request.method,
            upstream,
            params=dict(request.query_params),
            headers=headers,
            content=await request.body(),
        )
        response = await proxy_client().send(upstream_request, stream=True)
    except httpx.ConnectError:
        return _not_found(
            f"The service “{deployment.name}” is not accepting connections.",
            f"It must listen on 0.0.0.0:{deployment.port} inside the container.",
        )
    except httpx.HTTPError as exc:
        return _not_found(f"Upstream error from “{deployment.name}”.", str(exc))

    out_headers = {
        k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP
    }
    # Same origin, same problem as a published file — and here the upstream is a
    # process the account owner wrote, so it would set its own policy otherwise.
    out_headers["content-security-policy"] = USER_CONTENT_CSP
    out_headers["cache-control"] = USER_CONTENT_CACHE
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=out_headers,
        background=_closer(response),
    )


def _closer(response: httpx.Response):
    from starlette.background import BackgroundTask

    return BackgroundTask(response.aclose)


# `/@name` is the address people share. The `@` keeps the user namespace and the
# platform namespace permanently apart: no reserved-word list can stop someone
# registering a name that a future route needs, and this way none has to.
@router.api_route("/@{username}", methods=["GET", "HEAD"])
async def handle_root(
    request: Request, session: SessionDep, username: str, viewer: MaybeUser = None
) -> Response:
    return await serve_space(request, session, username, "", viewer)


@router.api_route(
    "/@{username}/{rest:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def handle_path(
    request: Request, session: SessionDep, username: str, rest: str,
    viewer: MaybeUser = None,
) -> Response:
    # `/@name/` lands here, not on handle_root, so the owner has to be resolved
    # in both places or the profile forgets who is looking at it.
    return await serve_space(request, session, username, rest, viewer)


# The original shape, kept forever. Links already handed out have to keep
# working, and there is no version of this product where breaking someone's
# published URL is acceptable.
@router.api_route("/u/{username}", methods=["GET", "HEAD"])
async def space_root(
    request: Request, session: SessionDep, username: str, viewer: MaybeUser = None
) -> Response:
    return await serve_space(request, session, username, "", viewer)


@router.api_route(
    "/u/{username}/{rest:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def space_path(
    request: Request, session: SessionDep, username: str, rest: str,
    viewer: MaybeUser = None,
) -> Response:
    return await serve_space(request, session, username, rest, viewer)
