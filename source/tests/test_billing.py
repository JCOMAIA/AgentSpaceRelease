"""Subscription entitlement logic.

Events are fabricated rather than mocked. `apply_event` was split out of the
signature-checking half precisely so that what an account is entitled to can be
tested exhaustively without Stripe, a network, or a mock library.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app import billing
from app.config import PLANS, get_settings
from app.db import session_scope
from app.models import Subscription, User


@pytest.fixture(autouse=True)
def price_ids(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_price_maker", "price_maker_test")
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro_test")


async def _user(username: str) -> User:
    async with session_scope() as session:
        return await session.scalar(select(User).where(User.username == username))


def subscription_event(
    user_id: str,
    *,
    event_id: str,
    status: str = "active",
    price: str = "price_maker_test",
    event_type: str = "customer.subscription.updated",
    cancel_at_period_end: bool = False,
) -> dict:
    # Derived from the user: `stripe_subscription_id` is UNIQUE, and the suite
    # shares one database, so a fixed id collides across tests.
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": f"sub_{user_id[:12]}",
                "customer": f"cus_{user_id[:12]}",
                "status": status,
                "cancel_at_period_end": cancel_at_period_end,
                "current_period_end": int(time.time()) + 30 * 86400,
                "metadata": {"agentspace_user_id": user_id},
                "items": {"data": [{"price": {"id": price}}]},
            }
        },
    }


async def _apply(event: dict) -> str:
    async with session_scope() as session:
        return await billing.apply_event(session, event)


async def _plan_of(username: str) -> str:
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == username))
        return user.plan


# --------------------------------------------------------------------------
# Entitlement
# --------------------------------------------------------------------------
async def test_paying_upgrades_the_plan(client, account):
    user = await _user(account["username"])
    assert await _plan_of(account["username"]) == "free"

    await _apply(subscription_event(user.id, event_id="evt_1"))
    assert await _plan_of(account["username"]) == "maker"


async def test_the_pro_price_maps_to_the_pro_plan(client, account):
    user = await _user(account["username"])
    await _apply(subscription_event(user.id, event_id="evt_2", price="price_pro_test"))
    assert await _plan_of(account["username"]) == "pro"


async def test_cancelling_returns_the_account_to_free(client, account):
    user = await _user(account["username"])
    await _apply(subscription_event(user.id, event_id="evt_3"))
    assert await _plan_of(account["username"]) == "maker"

    await _apply(
        subscription_event(
            user.id, event_id="evt_4", event_type="customer.subscription.deleted"
        )
    )
    assert await _plan_of(account["username"]) == "free"


async def test_a_failed_payment_does_not_cut_service_immediately(client, account):
    """Stripe retries for days; killing running services on the first miss
    loses the customer we were trying to keep."""
    user = await _user(account["username"])
    await _apply(subscription_event(user.id, event_id="evt_5"))

    await _apply(
        subscription_event(
            user.id, event_id="evt_6", event_type="invoice.payment_failed", status="past_due"
        )
    )
    assert await _plan_of(account["username"]) == "maker"

    async with session_scope() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == user.id))
        assert sub.status == "past_due"


async def test_an_unpaid_subscription_loses_access(client, account):
    user = await _user(account["username"])
    await _apply(subscription_event(user.id, event_id="evt_7"))
    await _apply(subscription_event(user.id, event_id="evt_8", status="unpaid"))
    assert await _plan_of(account["username"]) == "free"


async def test_scheduled_cancellation_keeps_access_until_the_period_ends(client, account):
    user = await _user(account["username"])
    await _apply(
        subscription_event(user.id, event_id="evt_9", cancel_at_period_end=True)
    )
    assert await _plan_of(account["username"]) == "maker"

    async with session_scope() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == user.id))
        assert sub.cancel_at_period_end is True


# --------------------------------------------------------------------------
# Idempotency — Stripe retries every non-2xx
# --------------------------------------------------------------------------
async def test_redelivering_an_event_changes_nothing(client, account):
    user = await _user(account["username"])
    event = subscription_event(user.id, event_id="evt_dup")

    assert await _apply(event) == "subscription_active"
    assert await _apply(event) == "duplicate:customer.subscription.updated"
    assert await _plan_of(account["username"]) == "maker"


async def test_a_retried_cancellation_cannot_undo_a_resubscription(client, account):
    """The ordering bug that would silently downgrade a paying customer."""
    user = await _user(account["username"])
    cancellation = subscription_event(
        user.id, event_id="evt_cancel", event_type="customer.subscription.deleted"
    )
    await _apply(subscription_event(user.id, event_id="evt_a"))
    await _apply(cancellation)
    assert await _plan_of(account["username"]) == "free"

    await _apply(subscription_event(user.id, event_id="evt_b"))
    assert await _plan_of(account["username"]) == "maker"

    await _apply(cancellation)  # Stripe retries the old delivery
    assert await _plan_of(account["username"]) == "maker"


async def test_an_unknown_event_type_is_recorded_but_ignored(client, account):
    user = await _user(account["username"])
    outcome = await _apply(
        subscription_event(user.id, event_id="evt_odd", event_type="customer.updated")
    )
    assert outcome == "ignored:customer.updated"
    assert await _plan_of(account["username"]) == "free"


async def test_an_event_for_nobody_does_not_crash(client):
    outcome = await _apply(
        {
            "id": "evt_orphan",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_x", "customer": "cus_unknown", "status": "active"}},
        }
    )
    assert outcome.startswith("ignored")


async def test_an_unrecognised_price_does_not_grant_a_plan(client, account):
    """A price added in Stripe but not configured here must not become an upgrade."""
    user = await _user(account["username"])
    await _apply(subscription_event(user.id, event_id="evt_px", price="price_not_ours"))
    assert await _plan_of(account["username"]) == "free"


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
async def test_plans_are_public_and_rendered_from_the_enforced_limits(client):
    res = await client.get("/api/v1/billing/plans")
    assert res.status_code == 200
    plans = {p["name"]: p for p in res.json()["data"]["plans"]}
    from app.billing import UNLISTED_PLANS

    assert set(plans) == set(PLANS) - UNLISTED_PLANS
    # Derived, not hard-coded: the point of this test is that the published
    # table comes from the objects that enforce the limits, so repricing a tier
    # must not require editing a test to match.
    for name in plans:
        plan = PLANS[name]
        assert plans[name]["price_display"] == plan.price_display
        assert plans[name]["limits"]["disk_mb"] == plan.disk_mb
        assert plans[name]["limits"]["max_upload_mb"] == plan.max_upload_mb
    assert plans["free"]["price_display"] == "Free"
    assert any(p["price_display"].startswith("€") for p in plans.values())


async def test_the_private_tier_is_not_on_the_price_list(client):
    """`personal` describes somebody's own machine and is not for sale.

    Listed publicly it puts a free 50 GB row beside a paid 5 GB one, which makes
    the thing being sold look like a joke.
    """
    res = await client.get("/api/v1/billing/plans")
    assert "personal" not in {p["name"] for p in res.json()["data"]["plans"]}


async def test_a_publish_only_instance_does_not_price_a_sandbox(client, monkeypatch):
    """Sandbox memory and run timeouts are not features where nothing runs."""
    from app import sandbox
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_driver", "none")
    sandbox.get_driver.cache_clear()
    try:
        res = await client.get("/api/v1/billing/plans")
        for plan in res.json()["data"]["plans"]:
            assert "memory_mb" not in plan["limits"], plan["name"]
            assert "exec_timeout_s" not in plan["limits"], plan["name"]
            assert plan["limits"]["disk_mb"] > 0
    finally:
        sandbox.get_driver.cache_clear()


async def test_an_api_key_cannot_start_a_subscription(client, account, monkeypatch):
    """An agent is lent a key to do work, not to commit its owner to spending."""
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_fake")
    # Registration leaves a session cookie on the client; drop it so the request
    # carries the API key and nothing else, which is the agent's situation.
    client.cookies.clear()

    res = await client.post(
        "/api/v1/billing/checkout", json={"plan": "maker"}, headers=account["headers"]
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "human_required"
    assert "dashboard" in res.json()["error"]["details"]


async def test_a_signed_in_human_gets_past_the_agent_check(client, monkeypatch):
    """The counterpart: the same call from a browser session is allowed through."""
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_fake")
    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "humanbuyer", "email": "humanbuyer@example.com",
              "password": "long-enough-password"},
    )
    assert reg.status_code == 200  # leaves a session cookie on the client

    res = await client.post("/api/v1/billing/checkout", json={"plan": "maker"})
    # It gets past the human gate and stops on infrastructure instead — which is
    # what this test is asserting. Which piece of infrastructure is missing
    # depends on whether the Stripe SDK is installed, so do not pin that.
    assert res.json()["error"]["code"] != "human_required"
    assert res.status_code == 503


async def test_subscription_state_is_visible_to_the_account(client, account):
    res = await client.get("/api/v1/billing/subscription", headers=account["headers"])
    data = res.json()["data"]
    assert data["plan"]["name"] == "free"
    assert data["status"] == "inactive"


async def test_webhook_without_a_signature_is_refused(client):
    res = await client.post("/api/v1/billing/webhook", json={"id": "evt_x", "type": "ping"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "missing_signature"


async def test_checkout_is_refused_when_billing_is_not_configured(client, monkeypatch):
    """A fresh install must say so plainly rather than 500."""
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "")
    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "buyer", "email": "buyer@example.com",
              "password": "long-enough-password"},
    )
    assert reg.status_code == 200
    res = await client.post("/api/v1/billing/checkout", json={"plan": "maker"})
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "billing_disabled"


async def test_an_unknown_plan_lists_the_real_ones(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_fake")
    reg = await client.post(
        "/api/v1/account/register",
        json={"username": "wrongplan", "email": "wrongplan@example.com",
              "password": "long-enough-password"},
    )
    assert reg.status_code == 200
    res = await client.post("/api/v1/billing/checkout", json={"plan": "enterprise"})
    assert res.status_code == 400
    assert "maker" in res.json()["error"]["fix"]
