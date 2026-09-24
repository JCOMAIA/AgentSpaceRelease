"""Application assembly."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import owner, publish_routes, reaper, web
from .a2a_server import agent_card
from .a2a_server import router as a2a_router
from .api import auth as auth_api
from .api import billing as billing_api
from .api import v1 as v1_api
from .config import get_settings
from .db import init_db, schema_drift, schema_status, session_scope
from .hosting import close_proxy_client, resolve_host
from .hosting import router as hosting_router
from .mcp_server import router as mcp_router
from .teaching import (
    AgentSpaceError,
    capability_table,
    execution_enabled,
    onboarding,
    onboarding_markdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agentspace")

# Paths that belong to the platform on every hostname, including user subdomains.
PLATFORM_PREFIXES = (
    "/api", "/mcp", "/a2a", "/.well-known", "/static", "/llms.txt", "/health",
    "/docs", "/openapi.json", "/redoc", "/register", "/login", "/logout", "/dashboard",
    "/publish", "/d/",
)


class SubdomainMiddleware:
    """Rewrite `alice.example.com/path` to the canonical `/@alice/path`.

    Done at the ASGI layer so every downstream route — including the static file
    handler and the service proxy — sees one consistent path shape.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "/")
        if path.startswith(PLATFORM_PREFIXES) or path.startswith(("/u/", "/@")):
            await self.app(scope, receive, send)
            return

        host = ""
        for key, value in scope.get("headers", []):
            if key == b"host":
                host = value.decode("latin-1")
                break
        if not host:
            await self.app(scope, receive, send)
            return

        try:
            async with session_scope() as session:
                username = await resolve_host(session, host)
        except Exception:  # a DB hiccup must not take down the apex site
            log.exception("host resolution failed for %r", host)
            username = None

        if username:
            scope = dict(scope)
            scope["path"] = f"/@{username}{path}"
            scope["raw_path"] = scope["path"].encode()
            scope["agentspace_vhost"] = username

        await self.app(scope, receive, send)


async def _verify_sandbox_runtime(settings) -> None:
    """Confirm the isolation we advertise is the isolation we will get.

    Silently falling back to runc when gVisor is missing would mean serving
    weaker isolation than the deployment promised, with nothing in the logs to
    say so. `SANDBOX_REQUIRE_RUNTIME=false` downgrades this to a warning for
    development boxes.
    """
    # Only the process that owns the daemon can inspect it. With the remote
    # driver that is the broker, which runs this same check at its own boot.
    if settings.sandbox_driver != "docker" or not settings.sandbox_runtime:
        return

    from .sandbox import SandboxUnavailable, get_driver

    driver = get_driver()
    try:
        await anyio.to_thread.run_sync(driver.verify_runtime)
    except SandboxUnavailable as exc:
        if settings.sandbox_require_runtime:
            raise RuntimeError(f"refusing to start: {exc}") from exc
        log.warning("%s — continuing with the default runtime because "
                    "SANDBOX_REQUIRE_RUNTIME is false", exc)
    else:
        log.info("sandbox runtime %r is available", settings.sandbox_runtime)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_root_abs.mkdir(parents=True, exist_ok=True)
    if settings.auto_migrate:
        await init_db()
    log.info("AgentSpace ready — public url %s, sandbox driver %s",
             settings.public_url, settings.sandbox_driver)
    if settings.secret_key == "dev-only-insecure-key":
        log.warning("SECRET_KEY is the default. Set a real one before exposing this server.")

    await _verify_sandbox_runtime(settings)

    current, head = await schema_status()
    log.info("database at revision %s", current or "none")
    if current != head:
        log.error(
            "DATABASE IS AT %s BUT HEAD IS %s. Migrations did not apply. Run "
            "`alembic upgrade head` before serving traffic.",
            current or "none",
            head or "none",
        )

    # A second, independent check: migrations can be at head and still not match
    # the models, which is what happens when someone edits a model and forgets to
    # generate the migration. That gap shows up here rather than as a 500 later.
    drift = await schema_drift()
    if drift:
        log.error(
            "MODELS AND DATABASE DISAGREE even at head: %s. A model was changed without a "
            "migration — generate one with `alembic revision --autogenerate -m \"...\"`.",
            "; ".join(drift),
        )

    if settings.single_user_mode:
        async with session_scope() as session:
            credentials = await owner.ensure_owner(session)
        if credentials is not None:
            log.info(
                "personal mode ready — credentials written to %s", owner.credentials_path()
            )
        else:
            log.info("personal mode — owner %r already exists", settings.owner_username)

    # The reaper only ever stops idle service containers. With no sandbox there
    # are none, so it would wake up forever to query a table that cannot change.
    reaper_task = (
        asyncio.create_task(reaper.run_forever()) if execution_enabled() else None
    )
    try:
        yield
    finally:
        if reaper_task is not None:
            reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await reaper_task
        await close_proxy_client()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AgentSpace",
        version="0.1.0",
        description=(
            "A persistent workspace, code sandbox and public hosting built for AI agents. "
            "Start at GET /api/v1/hello."
        ),
        lifespan=lifespan,
    )

    # ---- errors that teach ------------------------------------------------
    @app.exception_handler(AgentSpaceError)
    async def agentspace_error_handler(request: Request, exc: AgentSpaceError) -> JSONResponse:
        return JSONResponse(exc.as_dict(), status_code=exc.status_code)

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Any) -> JSONResponse | PlainTextResponse:
        if request.url.path.startswith(("/api", "/mcp", "/a2a")):
            return JSONResponse(
                AgentSpaceError(
                    "unknown_endpoint",
                    f"Nothing is served at {request.method} {request.url.path}.",
                    "GET /api/v1/hello returns the catalogue of everything this server does.",
                    status_code=404,
                ).as_dict(),
                status_code=404,
            )
        return PlainTextResponse("Not found", status_code=404)

    # ---- routers ----------------------------------------------------------
    app.include_router(v1_api.router)
    app.include_router(auth_api.router)
    app.include_router(billing_api.router)
    app.include_router(mcp_router)
    app.include_router(a2a_router)
    app.include_router(web.router)
    app.include_router(publish_routes.router)
    app.include_router(hosting_router)

    static_dir = web.STATIC_DIR
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ---- discovery surfaces ----------------------------------------------
    @app.get("/llms.txt", include_in_schema=False)
    async def llms_txt() -> PlainTextResponse:
        return PlainTextResponse(onboarding_markdown(), media_type="text/plain; charset=utf-8")

    @app.get("/.well-known/agentspace.json", include_in_schema=False)
    async def well_known() -> dict[str, Any]:
        s = get_settings()
        return {
            "name": "AgentSpace",
            "version": "0.1.0",
            "description": "Persistent workspace, code sandbox and hosting for agents.",
            "endpoints": {
                "rest": f"{s.public_url}/api/v1",
                "hello": f"{s.public_url}/api/v1/hello",
                "mcp": f"{s.public_url}/mcp",
                "a2a": f"{s.public_url}/a2a",
                "agent_card": f"{s.public_url}/.well-known/agent-card.json",
                "manual": f"{s.public_url}/llms.txt",
            },
            "authentication": {"scheme": "bearer", "header": "Authorization: Bearer ask_..."},
            "capabilities": capability_table(),
        }

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        # Browsers ask for this on every page regardless of the <link> tag;
        # answering keeps the access log free of phantom 404s.
        return RedirectResponse("/static/icon.svg", status_code=301)

    @app.get("/internal/tls-check", include_in_schema=False)
    async def tls_check(domain: str = "") -> PlainTextResponse:
        """Gate for Caddy's on-demand TLS.

        Without this, anyone could point a DNS record at the server and make it
        request certificates on their behalf until Let's Encrypt rate-limits us.
        Caddy is configured to ask here first and only issue on a 200.

        Keep this reachable only from the proxy — it is not on PLATFORM_PREFIXES
        by accident, and the compose network does not publish it.
        """
        from sqlalchemy import select

        from .models import User

        hostname = domain.strip().lower().rstrip(".")
        base = get_settings().base_domain.split(":", 1)[0].lower()
        if hostname == base or hostname.endswith(f".{base}"):
            return PlainTextResponse("ok", status_code=200)

        async with session_scope() as session:
            owner = await session.scalar(select(User).where(User.custom_domain == hostname))
        if owner is not None and owner.is_active:
            return PlainTextResponse("ok", status_code=200)

        log.info("refused on-demand TLS for unclaimed domain %r", hostname)
        return PlainTextResponse("unknown domain", status_code=404)

    # Registered last so it only catches genuinely unrouted /api/v1 paths:
    # an unknown endpoint should still teach rather than 404 blankly.
    @app.api_route(
        "/api/v1/{unknown_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def unknown_endpoint(request: Request, unknown_path: str) -> JSONResponse:
        raise AgentSpaceError(
            "unknown_endpoint",
            f"There is no {request.method} /api/v1/{unknown_path} in this API.",
            "Pick the closest capability from `details.capabilities`, "
            "or GET /api/v1/hello for the full briefing.",
            status_code=404,
            details={"capabilities": capability_table()},
        )

    app.add_middleware(SubdomainMiddleware)
    return app


app = create_app()

__all__ = ["app", "create_app", "agent_card", "onboarding"]
