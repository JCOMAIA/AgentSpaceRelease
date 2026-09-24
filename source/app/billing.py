"""Stripe subscriptions.

Split deliberately in two:

  * `verify_and_parse` does signature checking and needs the Stripe SDK.
  * `apply_event` takes an already-parsed event dict and changes our state.

That seam means the part that decides what a customer is entitled to can be
tested exhaustively with fabricated events, without a network, a Stripe account
or a mocking framework — and the part that cannot be tested locally is small
enough to read.

Stripe owns the money; this module owns what the platform will let an account
do. `User.plan` is written from the subscription so that quota checks stay a
local lookup rather than an API call on the hot path.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import PLANS, get_settings
from .models import BillingEvent, Subscription, User
from .teaching import AgentSpaceError

log = logging.getLogger(__name__)

# Events we act on. Anything else is recorded and ignored, so an unexpected
# event type can never silently change what someone is paying for.
HANDLED_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
}


def _stripe():
    settings = get_settings()
    if not settings.billing_enabled:
        raise AgentSpaceError(
            "billing_disabled",
            "Paid plans are not configured on this instance.",
            "The operator needs to set STRIPE_SECRET_KEY and the price IDs. "
            "The free plan works without any of this.",
            status_code=503,
        )
    try:
        import stripe
    except ImportError as exc:  # pragma: no cover - dependency present in prod
        raise AgentSpaceError(
            "billing_unavailable",
            "The Stripe SDK is not installed on this server.",
            "Install it with: pip install 'agentspace[billing]'",
            status_code=503,
        ) from exc
    stripe.api_key = settings.stripe_secret_key
    return stripe


async def get_or_create_subscription(session: AsyncSession, user: User) -> Subscription:
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    if subscription is None:
        subscription = Subscription(user_id=user.id, plan="free", status="inactive")
        session.add(subscription)
        await session.flush()
    return subscription


def plan_for_price(price_id: str) -> str | None:
    """Reverse the configured price IDs back to a plan name."""
    settings = get_settings()
    for name in PLANS:
        if price_id and settings.stripe_price_for(name) == price_id:
            return name
    return None


# --------------------------------------------------------------------------
# Outbound: checkout and the customer portal
# --------------------------------------------------------------------------
async def create_checkout(
    session: AsyncSession, user: User, plan_name: str, base_url: str
) -> str:
    plan = PLANS.get(plan_name)
    if plan is None or not plan.is_paid:
        raise AgentSpaceError(
            "unknown_plan",
            f"{plan_name!r} is not a paid plan.",
            f"Choose one of: {', '.join(n for n, p in PLANS.items() if p.is_paid)}.",
        )

    settings = get_settings()
    price_id = settings.stripe_price_for(plan_name)
    if not price_id:
        raise AgentSpaceError(
            "plan_not_configured",
            f"The {plan_name} plan has no Stripe price configured.",
            f"The operator needs to set STRIPE_PRICE_{plan_name.upper()}.",
            status_code=503,
        )

    stripe = _stripe()
    subscription = await get_or_create_subscription(session, user)

    if not subscription.stripe_customer_id:
        customer = stripe.Customer.create(
            email=user.email,
            name=user.username,
            # Lets us find the account from a Stripe dashboard row, and lets the
            # webhook attribute an event even if our own ids drift.
            metadata={"agentspace_user_id": user.id, "username": user.username},
        )
        subscription.stripe_customer_id = customer.id
        await session.flush()

    checkout = stripe.checkout.Session.create(
        mode="subscription",
        customer=subscription.stripe_customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base_url}/dashboard?checkout=success",
        cancel_url=f"{base_url}/dashboard?checkout=cancelled",
        client_reference_id=user.id,
        subscription_data={"metadata": {"agentspace_user_id": user.id}},
        allow_promotion_codes=True,
    )
    log.info("checkout created for user=%s plan=%s", user.username, plan_name)
    return checkout.url


async def create_portal(session: AsyncSession, user: User, base_url: str) -> str:
    """Stripe's own portal handles card changes, invoices and cancellation.

    Building those screens ourselves would mean handling card data; sending the
    customer to Stripe keeps this service entirely out of PCI scope.
    """
    subscription = await get_or_create_subscription(session, user)
    if not subscription.stripe_customer_id:
        raise AgentSpaceError(
            "no_billing_account",
            "This account has never started a subscription.",
            "Subscribe to a paid plan first; the billing portal appears afterwards.",
        )
    stripe = _stripe()
    portal = stripe.billing_portal.Session.create(
        customer=subscription.stripe_customer_id,
        return_url=f"{base_url}/dashboard",
    )
    return portal.url


# --------------------------------------------------------------------------
# Inbound: webhooks
# --------------------------------------------------------------------------
def verify_and_parse(payload: bytes, signature: str) -> dict[str, Any]:
    """Check Stripe's signature and return the event.

    Without this an unauthenticated POST could upgrade any account to Pro, so
    the signature is verified before the body is looked at, and a missing
    webhook secret is a hard failure rather than a skipped check.
    """
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise AgentSpaceError(
            "webhook_not_configured",
            "No webhook secret is set, so incoming events cannot be trusted.",
            "Set STRIPE_WEBHOOK_SECRET to the signing secret from the Stripe dashboard.",
            status_code=503,
        )
    stripe = _stripe()
    try:
        return stripe.Webhook.construct_event(
            payload, signature, settings.stripe_webhook_secret
        )
    except Exception as exc:
        raise AgentSpaceError(
            "invalid_signature",
            "That webhook did not carry a valid Stripe signature.",
            "Confirm the endpoint's signing secret matches STRIPE_WEBHOOK_SECRET.",
            status_code=400,
        ) from exc


def _period_end(raw: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC) if raw else None
    except (TypeError, ValueError, OSError):
        return None


async def _resolve_user(
    session: AsyncSession, *, user_id: str | None, customer_id: str | None
) -> User | None:
    if user_id:
        user = await session.get(User, user_id)
        if user is not None:
            return user
    if customer_id:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        if subscription is not None:
            return await session.get(User, subscription.user_id)
    return None


async def apply_event(session: AsyncSession, event: dict[str, Any]) -> str:
    """Apply a parsed Stripe event. Returns a short outcome for logging.

    Idempotent by event id: Stripe retries on any non-2xx, and a retried
    `subscription.deleted` must not downgrade an account that has since
    resubscribed.
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")

    if event_id and await session.get(BillingEvent, event_id):
        return f"duplicate:{event_type}"

    obj = (event.get("data") or {}).get("object") or {}
    customer_id = obj.get("customer")
    user_id = (obj.get("metadata") or {}).get("agentspace_user_id") or obj.get(
        "client_reference_id"
    )

    user = await _resolve_user(session, user_id=user_id, customer_id=customer_id)
    outcome = f"ignored:{event_type}"

    if user is not None and event_type in HANDLED_EVENTS:
        subscription = await get_or_create_subscription(session, user)
        if customer_id:
            subscription.stripe_customer_id = customer_id

        if event_type == "checkout.session.completed":
            subscription.stripe_subscription_id = obj.get("subscription")
            # The authoritative state arrives in customer.subscription.*; this
            # only records that checkout finished so the dashboard reacts fast.
            subscription.status = "active"
            outcome = "checkout_completed"

        elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
            subscription.stripe_subscription_id = obj.get("id")
            subscription.status = str(obj.get("status") or "inactive")
            subscription.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
            subscription.current_period_end = _period_end(obj.get("current_period_end"))
            items = (obj.get("items") or {}).get("data") or []
            price_id = items[0].get("price", {}).get("id") if items else None
            resolved = plan_for_price(price_id) if price_id else None
            if resolved:
                subscription.plan = resolved
            outcome = f"subscription_{subscription.status}"

        elif event_type == "customer.subscription.deleted":
            subscription.status = "canceled"
            subscription.cancel_at_period_end = False
            outcome = "subscription_canceled"

        elif event_type == "invoice.payment_failed":
            # Deliberately not a downgrade: Stripe runs its retry schedule and
            # sends dunning mail. Killing someone's running services on the first
            # failed charge loses the customer we were trying to keep.
            subscription.status = "past_due"
            outcome = "payment_failed"

        await sync_user_plan(session, user, subscription)

    if event_id:
        session.add(
            BillingEvent(
                id=event_id,
                type=event_type,
                user_id=user.id if user else None,
                payload=json.dumps(event)[:20_000],
            )
        )
    log.info("stripe event %s (%s) -> %s", event_id or "?", event_type, outcome)
    return outcome


async def sync_user_plan(
    session: AsyncSession, user: User, subscription: Subscription
) -> str:
    """Point `User.plan` at what the subscription currently entitles them to."""
    target = subscription.plan if subscription.grants_access else "free"
    if target not in PLANS:
        target = "free"
    if user.plan != target:
        log.info("plan change for %s: %s -> %s", user.username, user.plan, target)
        user.plan = target
        await session.flush()
    return target


async def describe(session: AsyncSession, user: User) -> dict[str, Any]:
    """What the dashboard and `whoami` show about billing."""
    settings = get_settings()
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    plan = PLANS.get(user.plan, PLANS["free"])
    return {
        "enabled": settings.billing_enabled,
        "currency": settings.billing_currency,
        "plan": {
            "name": plan.name,
            "title": plan.title,
            "price_cents": plan.price_cents,
            "price_display": plan.price_display,
        },
        "status": subscription.status if subscription else "inactive",
        "cancel_at_period_end": bool(subscription and subscription.cancel_at_period_end),
        "current_period_end": (
            subscription.current_period_end.isoformat()
            if subscription and subscription.current_period_end
            else None
        ),
        "has_billing_account": bool(subscription and subscription.stripe_customer_id),
    }


# Single-user mode's own tier. It is not for sale and its limits describe
# somebody's private machine, so listing it on a public pricing table puts a
# free 50 GB row next to the paid ones and makes them look ridiculous.
UNLISTED_PLANS = {"personal"}


def _size(mb: int) -> str:
    """Render a megabyte count the way a buyer reads it.

    Templates used to divide by 1024 inline, which turned the free plan's
    100 MB into "0 GB" -- the headline number of the tier most people land on,
    rendered as nothing. Doing it here keeps one answer for every template and
    for anyone rendering their own page off the JSON.
    """
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024} GB"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb} MB"


def public_plans() -> list[dict[str, Any]]:
    """The pricing table, rendered from the same objects that enforce limits."""
    from .teaching import execution_enabled

    executes = execution_enabled()
    out: list[dict[str, Any]] = []
    for plan in PLANS.values():
        if plan.name in UNLISTED_PLANS:
            continue
        limits: dict[str, Any] = {
            "disk_mb": plan.disk_mb,
            "disk_display": _size(plan.disk_mb),
            "max_upload_mb": plan.max_upload_mb,
            "max_upload_display": _size(plan.max_upload_mb),
            "max_deployments": plan.max_deployments,
            "max_api_keys": plan.max_api_keys,
            "custom_domain": plan.allow_custom_domain,
        }
        # Sandbox size and run timeouts are not features where nothing runs.
        # Printing them on a price list sells something that is not there.
        if executes:
            limits |= {
                "memory_mb": plan.memory_mb,
                "cpus": plan.cpus,
                "exec_timeout_s": plan.exec_timeout_s,
                "monthly_exec_seconds": plan.monthly_exec_seconds,
                "services_sleep_after_minutes": plan.idle_minutes,
            }
        out.append({
            "name": plan.name,
            "title": plan.title,
            "tagline": plan.tagline,
            "price_cents": plan.price_cents,
            "price_display": plan.price_display,
            "limits": limits,
        })
    return out
