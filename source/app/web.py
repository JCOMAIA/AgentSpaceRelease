"""Human-facing pages.

Everything that changes state goes through /api/v1/account/*, so these routes
only render. The dashboard is a small amount of vanilla JS against that same
public API — which doubles as a worked example of using it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from . import billing, quotas
from .config import get_settings
from .db import get_session
from .deps import optional_user
from .models import User
from .operations import space_urls
from .teaching import available_capabilities, execution_enabled
from .templating import build_templates

BASE_DIR = Path(__file__).parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

router = APIRouter(tags=["web"], include_in_schema=False)
templates = build_templates(TEMPLATE_DIR)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
MaybeUser = Annotated[User | None, Depends(optional_user)]


def _ctx(request: Request, user: User | None, **extra):
    s = get_settings()
    return {
        "request": request,
        "user": user,
        "settings": s,
        "base_domain": s.base_domain,
        "public_url": s.public_url,
        # Personal instances have no signup, no plans and nothing to buy, so the
        # templates drop every affordance that implies otherwise.
        "single_user": s.single_user_mode,
        # A publish-only space must not open by promising a sandbox. The first
        # sentence a stranger reads is the one they judge the whole thing by.
        "execution": execution_enabled(),
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, user: MaybeUser = None):
    return templates.TemplateResponse(
        request, "index.html", _ctx(request, user, capabilities=available_capabilities())
    )


@router.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request, user: MaybeUser = None):
    """Public, and deliberately not behind a login.

    The plan table used to live only on the dashboard, and only once a Stripe
    key was set -- so on a fresh instance nobody could find out what anything
    cost until after they had signed up for it.
    """
    if get_settings().single_user_mode:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "pricing.html",
        _ctx(
            request,
            user,
            plans=billing.public_plans(),
            billing_enabled=get_settings().billing_enabled,
        ),
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, user: MaybeUser = None):
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "register.html", _ctx(request, None))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: MaybeUser = None):
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", _ctx(request, None))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: SessionDep, user: MaybeUser = None):
    if not user:
        return RedirectResponse("/login", status_code=302)
    usage = await quotas.usage_summary(session, user)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            user,
            usage=usage,
            urls=space_urls(user),
            billing=await billing.describe(session, user),
            plans=billing.public_plans(),
        ),
    )
