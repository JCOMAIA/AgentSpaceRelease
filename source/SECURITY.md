# Security

AgentSpace runs untrusted code from strangers on purpose. That makes its security
properties the product, not a detail, so this document states them plainly — including
where they stop.

## Reporting a vulnerability

Email **outspacedev@gmail.com** — do not open a public issue.

Please include what you did, what happened, and what you expected. A proof of concept
helps enormously; a video does not.

- **Acknowledgement within 72 hours.** This is maintained by one person, so that is a
  realistic promise rather than an aspirational one.
- **Fix or a plan within 14 days** for anything that crosses a boundary listed below.
- **Credit in the release notes** unless you would rather not be named.

There is no bug bounty. There is genuine gratitude and a fast fix.

## What counts as a vulnerability

The boundaries this project claims to enforce, in the order that losing them would hurt:

1. **Sandbox → host.** Code executed through `/api/v1/exec` or a deployed service
   escaping its container, reading host paths, or reaching the Docker daemon.
2. **Tenant → tenant.** Any way to read, write or delete another account's files,
   deployments, keys or database rows.
3. **Control plane → host.** The control plane is designed *not* to hold the Docker
   socket. A path that gets it back, or that reaches the broker without its token, is a
   finding.
4. **Unauthenticated → authenticated.** Bypassing API-key or session authentication,
   forging a session cookie, or upgrading a plan without paying.
5. **Publishing what should stay private.** Any way to make a hidden file — `.env`,
   `.git`, `.ssh` — reachable through a published site.
6. **Sandbox escaping its resource ceiling** in a way that denies service to others.

## What is already known, and accepted

Reporting these is not useful; they are documented trade-offs, not oversights. If you
have found a way to make one of them *worse* than described, that very much is a report.

- **Sandboxes share the host kernel.** Containers are hardened (capabilities dropped,
  read-only rootfs, non-root uid, `no-new-privileges`, memory/CPU/pid limits, no network
  route out) and gVisor is the recommended runtime, but a kernel escape compromises every
  tenant. Removing this needs microVMs — see `docs/DEPLOY_KIMSUFI.md`.
- **The broker is root-equivalent when Docker is rootful.** It holds the Docker socket by
  design. Rootless Docker is documented and recommended; the broker warns on every boot
  when it is not in use.
- **A compromised control plane can run code as any user.** That is the boundary the
  broker draws: it costs you sandboxes, not the database, the host or the Stripe key.
- **Rate limits are per-process.** With multiple workers the effective ceiling is
  `limit × workers`. Documented in `app/ratelimit.py`.
- **The default deployment trusts one machine.** Database, control plane and sandboxes on
  one box is a deliberate small-scale choice, not an oversight.

## Deploying this safely

Run the checklist rather than reading it:

```bash
python -m app.cli preflight
```

It exits non-zero on anything critical, so a deploy can fail on it. With
`SANDBOX_DRIVER=remote` it asks the broker about the daemon it cannot see itself, so
"rootful daemon" still gets reported.

```
  ok    secret key               session cookies are signed with this
  ok    control plane privilege  does not hold the Docker socket
  WARN  rootless daemon          rootful — socket access is host root
  WARN  sandbox runtime          runc — user code shares the host kernel
  ok    registration             invite only

  0 critical, 2 warnings. Safe to expose.
```

`docs/DEPLOY_KIMSUFI.md` is still not optional reading. The short version:

- Set `SECRET_KEY` and `SANDBOX_BROKER_TOKEN` to real random values.
- Use `SANDBOX_DRIVER=remote` so the control plane never touches the Docker socket.
- Run the daemon rootless and set `SANDBOX_REQUIRE_ROOTLESS=true`.
- Set `SANDBOX_RUNTIME=runsc` and leave `SANDBOX_REQUIRE_RUNTIME=true`.
- Never publish the broker's port.
- Leave `SANDBOX_ALLOW_EGRESS=false` unless you have an allowlist. Enabling it gives
  anonymous strangers a machine to make outbound connections from.

## Supported versions

Pre-1.0. Only the latest commit on `main` receives fixes.
