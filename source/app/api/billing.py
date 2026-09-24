"""Billing endpoints.

Checkout and the portal are human-facing: an agent holding an API key must not
be able to move its owner onto a paid plan, so both require a session cookie.
The webhook is authenticated by Stripe's signature instead.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import billing
from ..config import PAID_PLANS, get_settings
from ..db import get_session
from ..deps import SESSION_COOKIE, current_user
from ..models import User
from ..security import read_session
from ..teaching import AgentSpaceError, Guide, NextStep, ok

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(current_user)]


class CheckoutBody(BaseModel):
    plan: str = Field(description=f"One of: {', '.join(PAID_PLANS)}")


async def human_user(request: Request, session: SessionDep) -> User:
    """Require a browser session, not an API key.

    Upgrading a plan spends the owner's money. An agent is lent an API key to do
    work in the space, not to commit its owner to a subscription.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    user_id = read_session(cookie) if cookie else None
    user = await session.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise AgentSpaceError(
            "human_required",
            "Billing changes need a signed-in browser session, not an API key.",
            "Open the dashboard and sign in. An agent cannot subscribe on your behalf.",
            status_code=403,
            details={"dashboard": f"{get_settings().public_url}/dashboard"},
        )
    return user


HumanDep = Annotated[User, Depends(human_user)]


@router.get("/plans", summary="The pricing table")
async def list_plans() -> dict[str, Any]:
    settings = get_settings()
    return ok(
        {
            "plans": billing.public_plans(),
            "currency": settings.billing_currency,
            "billing_enabled": settings.billing_enabled,
        },
        Guide(
            you_are_here="These are the plans and exactly what each one allows.",
            next_steps=[
                NextStep(
                    "Compare against what you are using now",
                    "Tells you whether an upgrade would actually change anything.",
                    {"transport": "rest", "method": "GET", "path": "/api/v1/whoami"},
                )
            ],
            notes=[
                "Limits shown here are the same objects the server enforces, "
                "so this table cannot drift from reality.",
                "Subscribing needs a human in a browser; an API key cannot do it.",
            ],
        ),
    )


@router.get("/subscription", summary="This account's billing state")
async def get_subscription(session: SessionDep, user: UserDep) -> dict[str, Any]:
    return ok(await billing.describe(session, user))


@router.post("/checkout", summary="Start a subscription")
async def checkout(session: SessionDep, user: HumanDep, body: CheckoutBody) -> dict[str, Any]:
    url = await billing.create_checkout(session, user, body.plan, get_settings().public_url)
    return ok(
        {"checkout_url": url, "plan": body.plan},
        Guide(
            you_are_here=f"Checkout ready for the {body.plan} plan.",
            next_steps=[],
            notes=["Open `checkout_url` to finish paying. The plan changes when Stripe "
                   "confirms it, not when the page closes."],
        ),
    )


@router.post("/portal", summary="Open the Stripe billing portal")
async def portal(session: SessionDep, user: HumanDep) -> dict[str, Any]:
    url = await billing.create_portal(session, user, get_settings().public_url)
    return ok(
        {"portal_url": url},
        Guide(
            you_are_here="Billing portal ready.",
            next_steps=[],
            notes=["Card details, invoices and cancellation all live there. "
                   "This server never sees a card number."],
        ),
    )


@router.post("/webhook", include_in_schema=False)
async def webhook(
    request: Request,
    session: SessionDep,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> dict[str, Any]:
    """Stripe's callback. Authenticated by signature, never by session.

    Returns 200 for anything successfully processed — including events we choose
    to ignore — because a non-2xx makes Stripe retry, and retrying an event we
    deliberately skipped achieves nothing.
    """
    if not stripe_signature:
        raise AgentSpaceError(
            "missing_signature",
            "No Stripe-Signature header on that request.",
            "This endpoint only accepts signed events from Stripe.",
            status_code=400,
        )

    payload = await request.body()
    event = billing.verify_and_parse(payload, stripe_signature)
    outcome = await billing.apply_event(session, event)
    return {"received": True, "outcome": outcome}
