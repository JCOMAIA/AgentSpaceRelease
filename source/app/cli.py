"""Operator commands.

    python -m app.cli status
    python -m app.cli invite create --count 10 --note "twitter thread"
    python -m app.cli user list
    python -m app.cli user suspend someone --reason "mining crypto"
    python -m app.cli user password someone

A command line rather than an admin web page, deliberately. An admin UI needs an
admin session, an admin role and an admin login form — three new ways into the
system, guarding data that one person needs to look at a few times a day. SSH is
already the trust boundary; this rides on it.

Every operation is a plain async function with the argparse layer kept thin, so
the behaviour can be tested without going through a shell.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio
from sqlalchemy import delete, func, select

from . import storage
from .accounts import purge_user
from .config import PLANS, get_settings
from .db import current_revision, head_revision, init_db, session_scope
from .models import ApiKey, Deployment, ExecSlot, Invite, Subscription, UsageEvent, User
from .security import hash_password

# No I, O, 0 or 1: these get read aloud and typed by hand.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate_code() -> str:
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{body[:4]}-{body[4:8]}-{body[8:]}"


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def human_bytes(size: float) -> str:
    """Rounding to megabytes prints "0.0 MB" for a real workspace, which reads
    as "empty" when it means "small"."""
    for unit, step in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= step:
            return f"{size / step:.1f} {unit}"
    return f"{int(size)} B"


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------
async def create_invites(count: int = 1, note: str = "", expires_days: int | None = None):
    expires_at = (
        datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None
    )
    codes = []
    async with session_scope() as session:
        for _ in range(max(1, count)):
            code = _generate_code()
            session.add(Invite(code=code, note=note, expires_at=expires_at))
            codes.append(code)
    return codes


async def list_invites(include_used: bool = False):
    async with session_scope() as session:
        query = select(Invite).order_by(Invite.created_at.desc())
        if not include_used:
            query = query.where(Invite.used_at.is_(None))
        invites = (await session.scalars(query)).all()
        users = {
            user.id: user.username
            for user in (
                await session.scalars(
                    select(User).where(
                        User.id.in_({i.used_by_user_id for i in invites if i.used_by_user_id})
                    )
                )
            ).all()
        }
    return [
        {
            "code": invite.code,
            "note": invite.note,
            "used_by": users.get(invite.used_by_user_id or "", ""),
            "expires_at": _aware(invite.expires_at),
        }
        for invite in invites
    ]


async def revoke_invite(code: str) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            delete(Invite).where(Invite.code == code.strip().upper(), Invite.used_at.is_(None))
        )
        return bool(result.rowcount)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
async def _get_user(session, username: str) -> User:
    user = await session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        raise SystemExit(f"no such user: {username}")
    return user


async def list_users():
    async with session_scope() as session:
        users = (await session.scalars(select(User).order_by(User.created_at))).all()
        rows = []
        for user in users:
            deployments = await session.scalar(
                select(func.count()).select_from(Deployment).where(Deployment.user_id == user.id)
            )
            keys = await session.scalar(
                select(func.count())
                .select_from(ApiKey)
                .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
            )
            last_used = await session.scalar(
                select(func.max(ApiKey.last_used_at)).where(ApiKey.user_id == user.id)
            )
            rows.append(
                {
                    "username": user.username,
                    "email": user.email,
                    "plan": user.plan,
                    "active": user.is_active,
                    "suspended_reason": user.suspended_reason or "",
                    "deployments": deployments or 0,
                    "keys": keys or 0,
                    "disk": human_bytes(storage.disk_usage(user.id)),
                    "last_seen": _aware(last_used),
                    "created_at": _aware(user.created_at),
                }
            )
    return rows


async def suspend_user(username: str, reason: str):
    async with session_scope() as session:
        user = await _get_user(session, username)
        user.is_active = False
        user.suspended_reason = reason
        return {"username": user.username, "active": False, "reason": reason}


async def restore_user(username: str):
    async with session_scope() as session:
        user = await _get_user(session, username)
        user.is_active = True
        user.suspended_reason = None
        return {"username": user.username, "active": True}


async def reset_password(username: str) -> str:
    """Set a fresh random password and return it.

    Until there is email there is no self-service reset, so a beta tester who
    forgets is otherwise locked out permanently. Hand this over on a channel
    you trust and tell them to change it.
    """
    new_password = secrets.token_urlsafe(12)
    async with session_scope() as session:
        user = await _get_user(session, username)
        user.password_hash = hash_password(new_password)
    return new_password


async def delete_user(username: str) -> dict:
    """Remove an account entirely — for a tester who asks, or test litter."""
    async with session_scope() as session:
        user = await _get_user(session, username)
        removed = await purge_user(session, user)
    return {"username": username, "workspace_bytes_removed": removed}


async def set_plan(username: str, plan: str):
    if plan not in PLANS:
        raise SystemExit(f"unknown plan: {plan}. Known: {', '.join(PLANS)}")
    async with session_scope() as session:
        user = await _get_user(session, username)
        previous = user.plan
        user.plan = plan
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user.id)
        )
    return {
        "username": username,
        "from": previous,
        "to": plan,
        # A manual grant is only stable while Stripe has no opinion; the next
        # webhook for a real subscriber overwrites it.
        "has_subscription": subscription is not None
        and subscription.stripe_subscription_id is not None,
    }


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
CRITICAL, WARNING, INFO = "critical", "warning", "info"


@dataclass
class Check:
    name: str
    ok: bool
    severity: str
    detail: str
    fix: str = ""


async def preflight() -> list[Check]:
    """The hardening checklist from SECURITY.md, executed rather than read.

    A checklist in a document is a thing you hope someone read. The same
    checklist as a command is a thing a deploy can fail on.
    """
    settings = get_settings()
    checks: list[Check] = []

    checks.append(
        Check(
            "secret key",
            settings.secret_key != "dev-only-insecure-key",
            CRITICAL,
            "session cookies are signed with this",
            'set SECRET_KEY — python -c "import secrets; print(secrets.token_urlsafe(48))"',
        )
    )

    driver = settings.sandbox_driver
    checks.append(
        Check(
            "sandbox driver",
            driver != "local_unsafe",
            CRITICAL,
            f"driver is {driver!r}",
            "local_unsafe runs user code unisolated on the host. Use 'remote'.",
        )
    )

    if driver == "none":
        checks.append(
            Check(
                "control plane privilege",
                True,
                INFO,
                "publish-only: no sandbox, no Docker socket, nothing to escape",
            )
        )
    elif driver == "remote":
        token = settings.sandbox_broker_token
        checks.append(
            Check(
                "broker token",
                len(token) >= 24,
                CRITICAL,
                "short or missing" if len(token) < 24 else f"{len(token)} chars",
                'set SANDBOX_BROKER_TOKEN — python -c "import secrets;'
                ' print(secrets.token_urlsafe(32))"',
            )
        )
        checks.append(
            Check(
                "control plane privilege",
                True,
                INFO,
                "does not hold the Docker socket",
            )
        )
    elif driver == "docker":
        checks.append(
            Check(
                "control plane privilege",
                False,
                WARNING,
                "this process talks to the Docker daemon directly",
                "set SANDBOX_DRIVER=remote and run app.broker as its own service, so a "
                "bug in a request handler costs a sandbox rather than the host",
            )
        )

    # The daemon lives behind whichever driver is in use. With `remote` the
    # control plane cannot see it at all, so the posture is fetched from the
    # broker — otherwise these checks silently vanish and a rootful host reads
    # as safe.
    if driver == "docker":
        checks.extend(await _daemon_checks(settings))
    elif driver == "remote":
        checks.extend(await _remote_daemon_checks(settings))

    if driver != "none":
        checks.append(
            Check(
                "sandbox egress",
                not settings.sandbox_allow_egress,
                WARNING,
                "user code can reach the internet" if settings.sandbox_allow_egress else "blocked",
                "SANDBOX_ALLOW_EGRESS=true gives strangers a machine to connect out from. "
                "Add an allowlist first.",
            )
        )

    checks.append(
        Check(
            "public url",
            settings.public_url.startswith("https://"),
            WARNING,
            settings.public_url,
            "session cookies are only marked Secure when PUBLIC_URL is https",
        )
    )

    # A full disk stops writes and corrupts the database, and it arrives quietly
    # — one user uploading video is enough. Worth seeing before it bites.
    try:
        usage = shutil.disk_usage(settings.data_root_abs)
        free_gb = usage.free / 1e9
        reserve_gb = settings.disk_reserve_mb / 1024
        checks.append(
            Check(
                "disk",
                free_gb > reserve_gb * 2,
                WARNING,
                f"{free_gb:.1f} GB free of {usage.total / 1e9:.0f} GB, "
                f"reserve {reserve_gb:.1f} GB",
                "free space or raise the disk. Writes stop at the reserve, and everything "
                "stops at zero.",
            )
        )
    except OSError:
        pass

    if settings.invite_required:
        registration = "invite only"
    elif settings.registration_open:
        registration = "open to the internet"
    else:
        registration = "closed"
    checks.append(
        Check(
            "registration",
            True,
            INFO,
            registration,
        )
    )

    current, head = await current_revision(), head_revision()
    checks.append(
        Check(
            "schema",
            current == head,
            CRITICAL,
            f"at {current or 'none'}, head is {head or 'none'}",
            "run: alembic upgrade head",
        )
    )

    if settings.billing_enabled:
        missing = [n for n in ("maker", "pro") if not settings.stripe_price_for(n)]
        checks.append(
            Check(
                "billing",
                not missing and bool(settings.stripe_webhook_secret),
                WARNING,
                f"missing: {', '.join(missing)}" if missing else "configured",
                "set the price ids and STRIPE_WEBHOOK_SECRET, or subscriptions will not "
                "apply when Stripe reports them",
            )
        )
    else:
        checks.append(Check("billing", True, INFO, "disabled; the free plan still works"))

    return checks


async def _remote_daemon_checks(settings) -> list[Check]:
    from .sandbox import SandboxUnavailable, get_driver

    try:
        posture = await get_driver().posture()
    except (SandboxUnavailable, ValueError, AttributeError) as exc:
        return [
            Check(
                "broker reachable",
                False,
                CRITICAL,
                str(exc)[:90],
                f"start it: uvicorn app.broker:app --port 9000 "
                f"(expected at {settings.sandbox_broker_url})",
            )
        ]

    checks = [Check("broker reachable", True, INFO, settings.sandbox_broker_url)]
    checks.append(
        Check(
            "rootless daemon",
            bool(posture.get("rootless")),
            WARNING,
            "rootless" if posture.get("rootless") else "rootful — socket access is host root",
            "run the daemon rootless under the broker and set "
            "SANDBOX_REQUIRE_ROOTLESS=true (docs/DEPLOY_KIMSUFI.md)",
        )
    )

    wanted = posture.get("runtime") or ""
    if wanted:
        checks.append(
            Check(
                "sandbox runtime",
                bool(posture.get("runtime_available")),
                CRITICAL,
                f"{wanted!r} "
                + ("available" if posture.get("runtime_available") else "NOT registered"),
                "install it on the broker's host; available there: "
                + ", ".join(posture.get("runtimes") or []),
            )
        )
    else:
        checks.append(
            Check(
                "sandbox runtime",
                False,
                WARNING,
                "runc — user code shares the host kernel",
                "set SANDBOX_RUNTIME=runsc on the broker for gVisor, or kata for a VM "
                "per sandbox",
            )
        )
    return checks


async def _daemon_checks(settings) -> list[Check]:
    from .sandbox import SandboxUnavailable, get_driver

    driver = get_driver()
    checks: list[Check] = []
    try:
        rootless = await anyio.to_thread.run_sync(driver.daemon_is_rootless)
        runtimes = await anyio.to_thread.run_sync(driver.available_runtimes)
    except (SandboxUnavailable, AttributeError) as exc:
        return [Check("docker daemon", False, WARNING, f"unreachable: {exc}",
                      "start Docker, or ignore this if the broker runs elsewhere")]

    checks.append(
        Check(
            "rootless daemon",
            rootless,
            WARNING,
            "rootless" if rootless else "rootful — socket access is host root",
            "run the daemon rootless and set SANDBOX_REQUIRE_ROOTLESS=true "
            "(docs/DEPLOY_KIMSUFI.md)",
        )
    )

    wanted = settings.sandbox_runtime
    if wanted:
        checks.append(
            Check(
                "sandbox runtime",
                wanted in runtimes,
                CRITICAL,
                f"{wanted!r} " + ("available" if wanted in runtimes else "NOT registered"),
                f"install it and register it with Docker; available: {', '.join(sorted(runtimes))}",
            )
        )
    else:
        checks.append(
            Check(
                "sandbox runtime",
                False,
                WARNING,
                "runc — user code shares the host kernel",
                "set SANDBOX_RUNTIME=runsc for gVisor, or kata for a VM per sandbox",
            )
        )
    return checks


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
async def status():
    settings = get_settings()
    now = datetime.now(UTC)
    async with session_scope() as session:
        users = await session.scalar(select(func.count()).select_from(User))
        suspended = await session.scalar(
            select(func.count()).select_from(User).where(User.is_active.is_(False))
        )
        by_plan = dict(
            (
                await session.execute(select(User.plan, func.count()).group_by(User.plan))
            ).all()
        )
        deployments = dict(
            (
                await session.execute(
                    select(Deployment.status, func.count()).group_by(Deployment.status)
                )
            ).all()
        )
        running_slots = await session.scalar(
            select(func.count()).select_from(ExecSlot).where(ExecSlot.expires_at > now)
        )
        used_mb = await session.scalar(
            select(func.coalesce(func.sum(ExecSlot.memory_mb), 0)).where(
                ExecSlot.expires_at > now
            )
        )
        exec_24h = await session.scalar(
            select(func.coalesce(func.sum(UsageEvent.amount), 0.0)).where(
                UsageEvent.kind == "exec_seconds",
                UsageEvent.created_at >= now - timedelta(hours=24),
            )
        )
        unused_invites = await session.scalar(
            select(func.count()).select_from(Invite).where(Invite.used_at.is_(None))
        )
        all_users = (await session.scalars(select(User.id))).all()

    disk_bytes = sum(storage.disk_usage(user_id) for user_id in all_users)
    return {
        "revision": await current_revision(),
        "users": {"total": users or 0, "suspended": suspended or 0, "by_plan": by_plan},
        "deployments": deployments,
        "sandbox_pool": {
            "running": running_slots or 0,
            "used_mb": int(used_mb or 0),
            "budget_mb": settings.exec_memory_budget_mb,
        },
        "exec_seconds_24h": round(exec_24h or 0.0, 1),
        "workspaces": human_bytes(disk_bytes),
        "unused_invites": unused_invites or 0,
        "registration": {
            "open": settings.registration_open,
            "invite_required": settings.invite_required,
        },
    }


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  " + "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  " + "  ".join("-" * widths[c] for c in columns)
    body = [
        "  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows
    ]
    return "\n".join([header, rule, *body])


def _short(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    return "" if value is None else str(value)


async def _dispatch(args: argparse.Namespace) -> int:
    await init_db()

    if args.command == "preflight":
        checks = await preflight()
        marks = {CRITICAL: "FAIL", WARNING: "WARN", INFO: "    "}
        width = max(len(c.name) for c in checks)
        print()
        for check in checks:
            mark = "ok  " if check.ok else marks[check.severity]
            print(f"  {mark}  {check.name.ljust(width)}  {check.detail}")
        failed = [c for c in checks if not c.ok and c.severity != INFO]
        if failed:
            print("\n  what to do:\n")
            for check in failed:
                print(f"    {check.name}: {check.fix}")
        criticals = [c for c in failed if c.severity == CRITICAL]
        warnings = [c for c in failed if c.severity == WARNING]
        print(
            f"\n  {len(criticals)} critical, {len(warnings)} warnings. "
            + ("Not safe to expose.\n" if criticals else "Safe to expose.\n")
        )
        return 1 if criticals else 0

    if args.command == "status":
        data = await status()
        print(f"\nschema revision  {data['revision']}")
        users = data["users"]
        plans = ", ".join(f"{k}={v}" for k, v in sorted(users["by_plan"].items())) or "none"
        print(f"users            {users['total']} ({plans}), {users['suspended']} suspended")
        deployed = ", ".join(f"{k}={v}" for k, v in sorted(data["deployments"].items()))
        print(f"deployments      {deployed or 'none'}")
        pool = data["sandbox_pool"]
        print(
            f"sandbox pool     {pool['running']} running, "
            f"{pool['used_mb']}/{pool['budget_mb']} MB"
        )
        print(f"compute (24h)    {data['exec_seconds_24h']}s")
        print(f"workspaces       {data['workspaces']}")
        print(f"invites unused   {data['unused_invites']}")
        reg = data["registration"]
        mode = (
            "invite only"
            if reg["invite_required"]
            else ("open" if reg["open"] else "closed")
        )
        print(f"registration     {mode}\n")
        return 0

    if args.command == "invite":
        if args.invite_command == "create":
            codes = await create_invites(args.count, args.note, args.expires_days)
            print(f"\n{len(codes)} invite code(s):\n")
            for code in codes:
                print(f"  {code}")
            print("\nSend one per tester. Each works once.\n")
        elif args.invite_command == "list":
            rows = [
                {**r, "expires_at": _short(r["expires_at"])}
                for r in await list_invites(include_used=args.all)
            ]
            print(f"\n{_table(rows, ['code', 'note', 'used_by', 'expires_at'])}\n")
        elif args.invite_command == "revoke":
            ok = await revoke_invite(args.code)
            print("revoked" if ok else "no such unused code")
            return 0 if ok else 1
        return 0

    if args.command == "owner":
        from .owner import credentials_path, mint_owner_key

        if args.owner_command == "key":
            async with session_scope() as session:
                key = await mint_owner_key(session, args.name)
            print(f"\nnew API key for {get_settings().owner_username}:\n\n  {key}\n")
            print("Shown once. Existing keys keep working; revoke them from the dashboard.\n")
        elif args.owner_command == "where":
            path = credentials_path()
            print(f"\n{path}")
            print("  exists\n" if path.exists() else "  missing — mint a new key instead\n")
        return 0

    if args.command == "user":
        if args.user_command == "list":
            rows = [
                {**r, "last_seen": _short(r["last_seen"]), "created_at": _short(r["created_at"])}
                for r in await list_users()
            ]
            columns = ["username", "plan", "active", "deployments", "keys", "disk",
                       "last_seen", "created_at"]
            print(f"\n{_table(rows, columns)}\n")
            for row in rows:
                if not row["active"]:
                    print(f"  {row['username']} suspended: {row['suspended_reason']}")
        elif args.user_command == "suspend":
            result = await suspend_user(args.username, args.reason)
            print(f"suspended {result['username']}: {result['reason']}")
            print("Their keys now fail with `account_suspended`, not `invalid_api_key`.")
        elif args.user_command == "restore":
            print(f"restored {(await restore_user(args.username))['username']}")
        elif args.user_command == "password":
            password = await reset_password(args.username)
            print(f"\nnew password for {args.username}:\n\n  {password}\n")
            print("Send it over a channel you trust and tell them to change it.\n")
        elif args.user_command == "delete":
            if not args.yes:
                confirm = input(f"permanently delete {args.username} and all data? [y/N] ")
                if confirm.strip().lower() not in ("y", "yes"):
                    print("cancelled")
                    return 1
            result = await delete_user(args.username)
            print(
                f"deleted {result['username']} "
                f"({human_bytes(result['workspace_bytes_removed'])} of workspace removed)"
            )
        elif args.user_command == "plan":
            result = await set_plan(args.username, args.plan)
            print(f"{result['username']}: {result['from']} -> {result['to']}")
            if result["has_subscription"]:
                print(
                    "WARNING: this account has a Stripe subscription. The next webhook "
                    "will overwrite this. Change the subscription in Stripe instead."
                )
        return 0

    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="one screen of how the instance is doing")
    sub.add_parser(
        "preflight",
        help="the SECURITY.md checklist, executed; exits non-zero on anything critical",
    )

    invite = sub.add_parser("invite", help="closed-beta invite codes")
    invite_sub = invite.add_subparsers(dest="invite_command", required=True)
    create = invite_sub.add_parser("create")
    create.add_argument("--count", type=int, default=1)
    create.add_argument("--note", default="", help="who it is for; you will forget")
    create.add_argument("--expires-days", type=int, default=None)
    listing = invite_sub.add_parser("list")
    listing.add_argument("--all", action="store_true", help="include used codes")
    revoke = invite_sub.add_parser("revoke")
    revoke.add_argument("code")

    own = sub.add_parser("owner", help="personal mode: the single account")
    own_sub = own.add_subparsers(dest="owner_command", required=True)
    own_key = own_sub.add_parser("key", help="mint a replacement API key")
    own_key.add_argument("--name", default="owner-key")
    own_sub.add_parser("where", help="path to the credentials file")

    user = sub.add_parser("user", help="accounts")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_sub.add_parser("list")
    suspend = user_sub.add_parser("suspend")
    suspend.add_argument("username")
    suspend.add_argument("--reason", required=True, help="shown to the account owner")
    user_sub.add_parser("restore").add_argument("username")
    user_sub.add_parser("password").add_argument("username")
    remove = user_sub.add_parser("delete", help="irreversible: containers, files and rows")
    remove.add_argument("username")
    remove.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    plan = user_sub.add_parser("plan")
    plan.add_argument("username")
    plan.add_argument("plan", choices=sorted(PLANS))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_dispatch(args))


if __name__ == "__main__":
    sys.exit(main())
