"""Application settings, loaded from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The env file is a variable rather than a constant so the test suite can
    # point it somewhere empty. Otherwise settings are read from whatever the
    # developer happens to have in .env, and a contributor with INVITE_REQUIRED
    # set watches unrelated tests fail for no visible reason.
    model_config = SettingsConfigDict(
        env_file=os.environ.get("AGENTSPACE_ENV_FILE", ".env"),
        extra="ignore",
    )

    base_domain: str = "localhost:8000"
    public_url: str = "http://localhost:8000"

    secret_key: str = "dev-only-insecure-key"

    database_url: str = "sqlite+aiosqlite:///./data/agentspace.db"
    data_root: Path = Path("./data/workspaces")

    # Whether `<username>.<base_domain>` actually resolves. Off by default
    # because the application cannot tell: it needs wildcard DNS and a wildcard
    # certificate, and behind a quick tunnel it never works. Advertising a URL
    # shape that does not resolve is worse than not advertising one that does —
    # the agent hands the dead link to a person.
    subdomain_urls: bool = False

    # Free space the host keeps back, above and beyond any plan. Per-user quotas
    # cap one workspace but never their sum, and a disk at 100% takes the
    # database with it. Writes are refused while free space is under this.
    disk_reserve_mb: int = 5120

    # Apply migrations from the application's own startup. Right for a single
    # dev process; wrong under `uvicorn --workers N`, where every worker would
    # race to apply the same revision. The Docker image runs them once in its
    # entrypoint and sets this false.
    auto_migrate: bool = True

    sandbox_driver: str = "docker"
    sandbox_image: str = "agentspace/runtime:latest"
    sandbox_network: str = "agentspace_sandbox"
    sandbox_allow_egress: bool = False
    # Docker runtime for user containers. "runsc" is gVisor, which puts a
    # userspace kernel between untrusted code and the host — the single biggest
    # isolation upgrade available without moving to VMs. Empty uses runc, the
    # default, which shares the host kernel directly.
    sandbox_runtime: str = ""
    # Refuse to boot if the configured runtime is missing, rather than silently
    # falling back to runc and serving weaker isolation than was promised.
    sandbox_require_runtime: bool = True

    # -- sandbox broker ---------------------------------------------------
    # With SANDBOX_DRIVER=remote the control plane never touches the Docker
    # socket. It asks a small privileged service for the seven operations in
    # SandboxDriver instead, and that service decides every dangerous parameter
    # itself. See app/broker.py and docs/ARCHITECTURE.md.
    sandbox_broker_url: str = ""
    sandbox_broker_token: str = ""
    # Ceilings the broker clamps every request to. A compromised control plane
    # can ask for anything; these bound what it can actually get.
    broker_max_memory_mb: int = 8192
    broker_max_cpus: float = 8.0
    broker_max_timeout_s: int = 1800
    # Refuse to start against a rootful daemon. Left false because most hosts
    # are rootful and a hard failure on a dev box helps nobody; turn it on in
    # production once rootless is set up, so a daemon that silently reverts
    # cannot go unnoticed. Off still warns on every boot.
    sandbox_require_rootless: bool = False

    # -- personal mode ----------------------------------------------------
    # One person, one workspace, no signup. The instance provisions its owner
    # on first boot and prints the key. Everything about accounts, plans and
    # billing exists for the hosted product and is switched off here, because
    # for someone running this for their own agent it is all ceremony.
    single_user_mode: bool = False
    owner_username: str = "me"

    registration_open: bool = True
    # Closed beta: registration needs a code from `python -m app.cli invite create`.
    # REGISTRATION_OPEN is the on/off switch; this is the "by invitation" middle
    # ground it could not express.
    invite_required: bool = False
    default_plan: str = "free"

    # -- billing ----------------------------------------------------------
    # Billing stays off until a secret key is present, so a fresh install and
    # the test suite never touch Stripe.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_maker: str = ""
    stripe_price_pro: str = ""
    billing_currency: str = "eur"

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    def stripe_price_for(self, plan_name: str) -> str:
        return {"maker": self.stripe_price_maker, "pro": self.stripe_price_pro}.get(plan_name, "")

    # -- capacity --------------------------------------------------------
    # Memory the box will hand out to concurrent sandbox executions at once.
    # Admission is refused past this, which is what stops a burst of `run_code`
    # calls from taking the machine down. Size it from free RAM minus what the
    # long-running services need; see docs/DEPLOY_KIMSUFI.md.
    #
    # Must be at least the largest plan's memory_mb, or that plan can never run
    # anything — a paying customer whose every execution is refused. The default
    # fits two concurrent Pro runs; `test_every_plan_fits_inside_the_default_budget`
    # fails if a plan is ever added that does not fit.
    exec_memory_budget_mb: int = 8192
    # Per-account ceiling, so one busy agent cannot occupy the whole budget.
    max_concurrent_execs_per_user: int = 2
    # How long a request will wait for a slot before giving up and telling the
    # agent to retry. Kept short: agents retry better than they wait.
    queue_wait_seconds: int = 15

    # Stop service containers with no traffic for this long. The next request
    # wakes them again. 0 disables reaping entirely.
    service_idle_minutes: int = 120
    reaper_interval_seconds: int = 300
    # How long to wait for a woken container to start listening again.
    service_wake_timeout_seconds: int = 20

    @property
    def data_root_abs(self) -> Path:
        return self.data_root.expanduser().resolve()


@dataclass(frozen=True)
class Plan:
    """Resource envelope for a pricing tier.

    Every limit here is enforced somewhere: disk in `storage`, memory/cpu in the
    sandbox driver, counts in `quotas`. `price_cents` is what Stripe charges;
    the two must not drift, so the checkout flow reads the tier from here and
    only the Price ID lives in Stripe.
    """

    name: str
    title: str
    tagline: str
    price_cents: int  # per month, in the billing currency
    disk_mb: int
    memory_mb: int
    cpus: float
    max_deployments: int
    max_api_keys: int
    exec_timeout_s: int
    max_upload_mb: int
    monthly_exec_seconds: int
    allow_custom_domain: bool
    idle_minutes: int | None = None  # None = use the global default

    @property
    def is_paid(self) -> bool:
        return self.price_cents > 0

    @property
    def price_display(self) -> str:
        return "Free" if not self.is_paid else f"€{self.price_cents / 100:.0f}"


# Sized against one small VPS: 4 GB RAM, 40 GB disk, ~10 EUR a month.
#
# Disk is the binding constraint, not memory, and the arithmetic is worth writing
# down because it is what decides when to buy a bigger box:
#
#     40 GB total
#    -2.6 GB  OS and packages
#    -5.0 GB  DISK_RESERVE_MB, the floor that stops a full disk killing SQLite
#    -------
#    32.4 GB  for user files AND their backups
#             backups are hard-linked over 14 days, so roughly 1.3x the files
#     ~12 GB  of actual user files
#
# At full quota that is ~120 free accounts, or ~10 paying ones. In practice a
# published page with its CSS and images is a few megabytes, so the real ceiling
# is in the hundreds of accounts and the trigger to upgrade is simple: when
# `du -sh data/workspaces` passes 8 GB, buy more disk.
#
# The execution fields below are inert wherever SANDBOX_DRIVER=none, which is
# the recommended shape for a public box. They stay so the same plan table works
# unchanged if a deployment does enable a sandbox.
PLANS: dict[str, Plan] = {
    "free": Plan(
        name="free",
        title="Free",
        tagline="Enough to put something real on the web.",
        price_cents=0,
        disk_mb=100,
        memory_mb=512,
        cpus=0.5,
        max_deployments=1,
        max_api_keys=3,
        exec_timeout_s=60,
        max_upload_mb=10,
        monthly_exec_seconds=3600,
        allow_custom_domain=False,
        idle_minutes=60,
    ),
    "maker": Plan(
        name="maker",
        title="Maker",
        tagline="Room for a site you keep adding to.",
        price_cents=300,
        disk_mb=1024,
        memory_mb=2048,
        cpus=2.0,
        max_deployments=5,
        max_api_keys=10,
        exec_timeout_s=300,
        max_upload_mb=50,
        monthly_exec_seconds=100_000,
        allow_custom_domain=True,
        idle_minutes=240,
    ),
    "pro": Plan(
        name="pro",
        title="Pro",
        tagline="Images, video, and a domain of your own.",
        price_cents=900,
        disk_mb=5120,
        memory_mb=4096,
        cpus=4.0,
        max_deployments=20,
        max_api_keys=30,
        exec_timeout_s=900,
        max_upload_mb=200,
        monthly_exec_seconds=500_000,
        allow_custom_domain=True,
        idle_minutes=0,  # never reaped: paying for it means it stays warm
    ),
}

PAID_PLANS = [name for name, plan in PLANS.items() if plan.is_paid]

# Personal mode's only plan. The limits are generous rather than absent: an agent
# in a loop can still fill a disk, and a ceiling that stops it is a feature even
# when there is nobody to bill.
PLANS["personal"] = Plan(
    name="personal",
    title="Personal",
    tagline="Your machine, your agent, no accounts.",
    price_cents=0,
    disk_mb=51_200,
    # 2 GB per sandbox is generous for an agent's script and leaves room on a
    # laptop. It must also fit inside EXEC_MEMORY_BUDGET_MB, or nothing can run
    # at all — see the budget check in scheduler.acquire.
    memory_mb=2048,
    cpus=4.0,
    max_deployments=50,
    max_api_keys=25,
    exec_timeout_s=900,
    max_upload_mb=1024,
    monthly_exec_seconds=10_000_000,
    allow_custom_domain=True,
    idle_minutes=0,  # it is your own machine; nothing needs to sleep
)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def plan_for(name: str) -> Plan:
    return PLANS.get(name, PLANS["free"])
