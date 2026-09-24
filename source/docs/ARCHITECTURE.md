# Architecture

## The shape of it

```
                    ┌──────────────────────────────────────────┐
   agent ──REST───► │                                          │
   agent ──MCP────► │            app/operations.py             │──► app/storage.py
   agent ──A2A────► │        every capability lives here       │      (path guard)
                    │                                          │──► app/sandbox/
                    └──────────────────────────────────────────┘      (driver)
                                       │
   browser ─────────────────────────►  │  app/hosting.py ──► static files
                                       │                 └─► reverse proxy ──► user container
```

Three protocol adapters, one implementation. `app/api/v1.py`, `app/mcp_server.py` and
`app/a2a_server.py` parse their own wire format and then call the same functions in
`app/operations.py`. None of them contains business logic.

That is the constraint that keeps the product promise true. "Write over MCP, read over
REST, same bytes" is not an integration to maintain — it is the only thing the code can
do.

## Why the teaching layer is a module, not a docs site

`app/teaching.py` owns three things:

- `Guide` / `NextStep` — the `guide` block on every successful response.
- `AgentSpaceError` — errors whose constructor *requires* a `fix` argument. An error that
  cannot say how to recover does not compile into existence.
- `CAPABILITIES` — one tuple that renders into the REST catalogue, the MCP tool list, the
  A2A agent card skills, and `/llms.txt`.

Documentation that lives beside the code drifts from it. Documentation generated *from*
the code cannot. Adding a capability means adding one entry to `CAPABILITIES` and one
branch to `operations.dispatch`; all four surfaces update together.

## Request lifecycle

A request arrives at `SubdomainMiddleware` (`app/main.py`). It looks at `Host`:

- apex domain → pass through untouched
- `alice.example.com` → rewrite the path to `/@alice/...`
- a claimed custom domain → same rewrite, after a database lookup

Platform paths (`/api`, `/mcp`, `/a2a`, `/.well-known`, …) are exempt, so the API answers
identically on every hostname. Everything downstream — routing, static serving, the
service proxy — sees one canonical path shape.

Authentication (`app/deps.py`) accepts a bearer API key or a signed session cookie and
resolves both to a `User`. Agents use the first; the dashboard uses the second.

## Path safety

`app/storage.py:resolve()` is the only function in the system permitted to turn an
untrusted string into a filesystem path. Every file operation, every deployment source
directory and every zip member goes through it.

It applies chroot semantics: the workspace root *is* the root. `/etc/passwd` lands at
`<workspace>/etc/passwd`; `/workspace/app.py` — the prefix we advertise to agents — lands
at `<workspace>/app.py`. Traversal above the root is rejected, after `resolve(strict=False)`
collapses any symlinks in the existing prefix.

One guard, one test file, one place to audit.

## Sandbox drivers

`app/sandbox/base.py` defines six methods. `DockerDriver` implements them today;
Firecracker or gVisor would implement the same six.

The hardening applied to every container is in `_common_kwargs`: capabilities dropped,
read-only rootfs, non-root uid, `no-new-privileges`, memory/CPU/pid ceilings, and
`network_mode=none` unless the plan allows egress. `tests/test_docker_sandbox.py` asserts
each of those properties against a real daemon rather than trusting the flags were passed.

`LocalUnsafeDriver` runs code as a subprocess with no isolation at all. It exists so CI
and a laptop without Docker can exercise the rest of the system, and `get_driver()` will
only select it when an operator sets `SANDBOX_DRIVER=local_unsafe` by hand.

## Deployments

Two kinds, deliberately:

**static** — a row in the database pointing at a workspace directory. No container, no
build step. Editing the files changes the live site immediately, because the files *are*
the site. Costs nothing but disk.

**service** — a long-lived container on the internal `agentspace_sandbox` network running
the user's command. `hosting.py` proxies to `container_name:port`. The network is
`internal: true`, so the proxy can reach the container and the container can reach nothing.

Publishing a static site is one call and cannot fail at runtime; that is the path most
agents should take, so the guides steer toward it.

## Capacity control

Two mechanisms keep a single box from being taken down by ordinary use.

**Admission control** (`app/scheduler.py`). `run_code` does not start a container until a
slot fits inside a memory budget. Two limits are checked together: a global budget, which
is the real constraint on one machine, and a per-account count, so one busy agent cannot
crowd out everyone else. A refusal distinguishes the two cases, because the fixes differ —
"wait for your own run to finish" versus "the server is loaded, retry".

Slots live in the `exec_slots` table, not in process memory. A per-process semaphore would
multiply the budget by the uvicorn worker count, which is the opposite of a limit. Rows
carry an expiry, so a worker killed mid-run releases its slot without a sweeper.

The limiter is approximate by design: two workers can pass the check simultaneously and
overshoot by a slot each. That is a capacity guard with headroom, not a security boundary,
and `SELECT FOR UPDATE` on every execution would cost more than the overshoot.

**The idle reaper** (`app/reaper.py`). A service container holds its full memory ceiling
whether or not anyone visits, and most free-tier services are visited by nobody. The
reaper stops services with no traffic for `service_idle_minutes`; the next request to one
restarts it and is served after the wait. Idling therefore costs latency, not
availability, which is the only version of this that does not break the product promise.

Waking has one non-obvious requirement: an ephemeral published port is *not* preserved
across a container restart, so `wake_service` re-reads the binding and the new port is
written back. Skipping that turns every woken service into a silent 404.

Both write through the caller's session rather than opening their own. A second connection
nested inside a request's open transaction deadlocks on SQLite, and committing before the
container runs is what stops a 60-second execution from holding a write lock.

## Billing

`app/billing.py` is split at a deliberate seam:

- `verify_and_parse` checks Stripe's signature. Needs the SDK, cannot be tested locally.
- `apply_event` takes an already-parsed event and decides what the account may do.

Everything interesting lives on the second side, so entitlement — upgrades, cancellation,
failed payments, retried deliveries — is tested exhaustively with fabricated events and no
network, no Stripe account and no mocking library. The untestable half is small enough to
read in one sitting.

Stripe owns the money; `User.plan` owns what the platform permits. The webhook reconciles
them, which keeps quota checks a local lookup instead of an API call on the hot path.

Two properties that had to be designed rather than fallen into:

- **Idempotency.** Stripe retries every non-2xx. `BillingEvent` records each event id, so a
  redelivered `subscription.deleted` cannot downgrade an account that has since
  resubscribed. There is a test for exactly that ordering.
- **An API key cannot spend money.** Checkout and the portal require a browser session.
  An agent is lent a key to do work in the space, not to commit its owner to a subscription.

The pricing table is rendered from the same `Plan` objects that enforce the limits, so the
page and the enforcement cannot disagree.

## Schema ownership

Alembic owns the schema, in development and in the test suite as much as in production.
`init_db()` runs `alembic upgrade head` rather than `create_all`, so the migration path is
exercised on every test run instead of being discovered broken at deploy time.

This replaced `create_all`, which adds tables but never columns — a gap that let a model
change reach a running instance and turn every request touching it into an opaque 500.

Two checks survive at boot because they catch different mistakes:

- `schema_status()` compares the database's revision to the newest script. Catches
  *migrations were not applied*.
- `schema_drift()` compares the models to the actual columns. Catches *a model was changed
  and no migration was written* — which the revision check cannot see, since the database
  is legitimately at head.

`tests/test_migrations.py` runs `compare_metadata` against a database built only by
migrations, so that second failure is caught in CI rather than at boot.

## The privilege boundary

Holding the Docker socket is equivalent to holding root on the host: anything
that can reach it can start a container that bind-mounts `/` and chroots into
it. That is not an exploit, it is the API working as designed.

The control plane parses untrusted input all day, so it does not get that power.
`app/broker.py` is a separate service that holds the socket and exposes exactly
the seven methods of `SandboxDriver` — the same protocol `DockerDriver`
implements, which is why the boundary cost almost no new abstraction.

```
control plane ──HTTP──► broker ──socket──► docker daemon ──► sandbox container
 (no socket)             (socket)
```

`RemoteDriver` implements `SandboxDriver` over that HTTP hop, so `operations.py`
cannot tell which driver it holds and nothing above the seam changed.

What makes it a boundary rather than a hop:

- **Paths are derived, never accepted.** The workspace path is computed from the
  user id. There is no request field that can aim a mount at `/etc`, because the
  field does not exist.
- **Resources are clamped.** A caller asking for a terabyte gets the ceiling.
- **Ids are validated.** `user_id` must be hex; names and source directories are
  checked for traversal before they can become a mount.
- **It is not published.** The broker sits on an `internal: true` network with no
  `ports:` entry, and refuses to start without a shared secret.

A compromised control plane can still run code as any user, which is bad. It
cannot read the database, reach the host filesystem, or start a privileged
container, which is the difference between an incident and losing the machine.

`tests/test_broker.py` is written from the attacker's side — it asks the broker
for the escapes and asserts the refusals. One test in `test_docker_sandbox.py`
drives the whole chain into a real container, because the boundary tests replace
Docker with a recorder and would not notice the wiring being wrong.

## Security decisions

- **Hidden files are never published.** Publishing a directory publishes everything in it,
  and that is very often `.env` or `.git/config`. Rather than guessing which files are
  sensitive, any path with a dot-prefixed segment is refused — before checking whether it
  exists, so the answer does not leak that either. Credential-shaped files that are *not*
  hidden are named back to the agent in the deploy response, because a warning that lists
  the files is actionable and a generic one is not.
- **Rate limits live in process memory.** A limiter that writes a row per attempt hands an
  attacker a cheap way to hammer the disk. The cost is that the ceiling is
  `limit x workers`; the interface is small enough to move to Redis when that matters.
- **Deletion is deletion.** `DELETE /api/v1/account` stops containers, removes the
  workspace from disk and deletes every row. No soft-delete, no grace period. The password
  is the gate rather than the session, so a borrowed API key cannot destroy the account.
- **The runtime is verified at boot.** With `SANDBOX_RUNTIME=runsc` the app refuses to
  start if gVisor is missing, instead of falling back to `runc` and serving weaker
  isolation than the deployment promised.

## What is deliberately missing

- **Email verification.** Registration is rate-limited per IP, and a paid plan is verified
  by the card. A free tier with no email check is still the obvious abuse lever, and closing
  it needs an SMTP decision that has not been made.
- **Usage-based billing.** `UsageEvent` records what a meter would need. Nothing reads it
  for money; plans are flat-rate.

These are listed rather than stubbed because a stub implies a decision that has not been
made.
