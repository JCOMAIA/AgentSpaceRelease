"""Database models.

One workspace per user for now, but `Workspace` is its own row so that teams or
multiple environments per account are an additive change later.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(39), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(32), default="free")
    custom_domain: Mapped[str | None] = mapped_column(String(255), unique=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Why access was withdrawn. Shown to the account, so it is written for them
    # to read, not as an internal note.
    suspended_reason: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user", lazy="selectin")
    workspace: Mapped[Workspace] = relationship(
        back_populates="user", lazy="selectin", uselist=False
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="default")
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped[User] = relationship(back_populates="api_keys", lazy="selectin")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    disk_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="workspace", lazy="selectin")


class Deployment(Base):
    """Something the agent published: a static site or a long-running service."""

    __tablename__ = "deployments"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_deployment_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))  # "static" | "service"
    source_dir: Mapped[str] = mapped_column(String(512))
    # service-only fields
    command: Mapped[str | None] = mapped_column(Text, default=None)
    # `port` is what the user declared their process listens on — echoed back to
    # them and reused on redeploy. `internal_port` is where the proxy actually
    # connects, which differs whenever the driver publishes to the host.
    port: Mapped[int | None] = mapped_column(Integer, default=None)
    container_id: Mapped[str | None] = mapped_column(String(64), default=None)
    internal_host: Mapped[str | None] = mapped_column(String(255), default=None)
    internal_port: Mapped[int | None] = mapped_column(Integer, default=None)

    # "live" (static) | "running" | "idle" (reaped, wakes on request) | "stopped"
    status: Mapped[str] = mapped_column(String(16), default="stopped")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Written by the proxy, coarsely — see hosting.touch_deployment. Drives the
    # idle reaper, so it only needs to be right to within a few minutes.
    last_request_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ExecJob(Base):
    __tablename__ = "exec_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentTask(Base):
    """A2A task state. Kept server-side so `tasks/get` survives reconnects."""

    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    context_id: Mapped[str] = mapped_column(String(32), index=True, default=_uuid)
    state: Mapped[str] = mapped_column(String(24), default="submitted")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Invite(Base):
    """A single-use registration code.

    A closed beta needs something between "open to the internet" and "closed to
    everyone", which is all `REGISTRATION_OPEN` could express. The note field
    exists because by tester number fifteen you will not remember who a code
    was for.
    """

    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    used_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_used(self) -> bool:
        return self.used_at is not None


class Subscription(Base):
    """What Stripe believes about this account, mirrored locally.

    Stripe is the source of truth for money; this table is the source of truth
    for what the platform will let the account do. They are reconciled by the
    webhook, and `User.plan` is written from here so that quota checks never
    need a network call.
    """

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, default=None
    )
    plan: Mapped[str] = mapped_column(String(32), default="free")
    # Stripe's vocabulary: active, trialing, past_due, canceled, unpaid, incomplete.
    status: Mapped[str] = mapped_column(String(32), default="inactive")
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def grants_access(self) -> bool:
        """Whether this subscription should currently unlock its plan.

        `past_due` still counts: a failed card should trigger dunning emails,
        not an instant shutdown of someone's running services.
        """
        return self.status in ("active", "trialing", "past_due")


class BillingEvent(Base):
    """Every Stripe webhook we accepted, by Stripe's event id.

    Stripe retries deliveries, so handlers must be idempotent. Recording the id
    is what makes that true rather than hoped-for.
    """

    __tablename__ = "billing_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Stripe's evt_...
    type: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExecSlot(Base):
    """One in-flight sandbox execution, holding its share of the memory budget.

    Lives in the database rather than in process memory so the limit holds
    across uvicorn workers. Rows carry an expiry, so a worker that dies mid-run
    releases its slot without anyone cleaning up after it.
    """

    __tablename__ = "exec_slots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    memory_mb: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class UsageEvent(Base):
    """Append-only meter. Monthly quota checks aggregate over this."""

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # exec_seconds | egress_bytes | ...
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
