# Deploying AgentSpace on a Kimsufi box

Target: one Kimsufi server — Xeon, 32 GB RAM, 4×2 TB SATA, ~20 EUR/month — running the
whole stack. This document is what to do, plus what that choice costs you.

---

## 1. Read this before anything else

**Only the broker holds the Docker socket.** Anything that can reach
`/var/run/docker.sock` can start a container that bind-mounts `/` and chroots into it —
that is the API working as designed, not a bug. So the control plane, which parses
untrusted input all day, does not get it. `app/broker.py` runs as its own service, holds
the socket, and exposes exactly the seven sandbox operations, deriving every dangerous
parameter itself.

Compromising the control plane therefore costs you the ability to run code as any user,
not the machine. The database, the Stripe key and the TLS keys stay out of reach.

**Two risks remain, and you should know both:**

1. **The broker is still a root-equivalent service.** It is small and its input is
   validated, but a bug there is a host compromise. Keep it on the internal network, never
   publish its port, and set `SANDBOX_BROKER_TOKEN`.
2. **Sandboxes share the host kernel.** They are hardened — no capabilities, read-only
   rootfs, non-root uid, `no-new-privileges`, memory/CPU/pid limits, no network route out —
   and with gVisor a userspace kernel sits in between. A kernel escape is still a full
   compromise of every tenant.

For a free tier on one box this is a defensible trade. Three things make it defensible
rather than reckless:

1. Nothing else valuable lives on this machine. No personal email, no other projects.
2. You can afford to rebuild it from scratch. Keep the deploy reproducible.
3. You tell users what they are getting. Do not advertise isolation you do not have.

### Rootless Docker

This is the change that makes socket access stop meaning host root. The daemon runs as an
unprivileged user, so anything that compromises the broker gets that user's privileges
instead of the machine.

```bash
adduser --disabled-password --gecos "" agentspace
loginctl enable-linger agentspace        # keeps the daemon alive without a session
apt install -y uidmap dbus-user-session

su - agentspace -c 'curl -fsSL https://get.docker.com/rootless | sh'
```

Give the account cgroup delegation, or memory and CPU limits silently stop applying and
the capacity plan in section 3 becomes fiction:

```bash
mkdir -p /etc/systemd/system/user@.service.d
cat > /etc/systemd/system/user@.service.d/delegate.conf <<'EOF'
[Service]
Delegate=cpu cpuset io memory pids
EOF
systemctl daemon-reload
```

Start it and confirm:

```bash
su - agentspace -c 'systemctl --user enable --now docker'
su - agentspace -c 'docker info --format "{{.SecurityOptions}}"'   # must contain rootless
```

Then point the broker at that socket and make the workspaces belong to the same account:

```bash
chown -R agentspace:agentspace /srv/agentspace/workspaces
```

```yaml
# docker-compose.yml, broker service
environment:
  DOCKER_HOST: unix:///run/user/1001/docker.sock   # id -u agentspace
volumes:
  - /run/user/1001/docker.sock:/run/user/1001/docker.sock
```

Finally set `SANDBOX_REQUIRE_ROOTLESS=true`. The broker verifies this at every boot and
refuses to start against a rootful daemon, so a host that reverts cannot do so quietly.
With it unset the broker still logs a warning on every start — the situation is allowed to
be true, not allowed to be invisible.

**Verify the limits survived**, because this is the part that fails silently:

```bash
pytest tests/test_docker_sandbox.py -k memory   # must still pass
```

### What the broker container itself gets

Read-only rootfs, all capabilities dropped, `no-new-privileges`, and a 32 MB tmpfs. None
of that stops socket abuse — rootless is the answer to that — but it raises the cost of
every other kind of bug in the service.

It deliberately does **not** receive `.env`. The database password, the session key and
the Stripe key have no use here, and handing them to the most privileged service in the
stack would defeat the boundary it exists to create. `tests/test_compose.py` asserts this,
because it was wrong once.

The upgrade path, in order of cost:

- **gVisor** (`runsc`) as the runtime — configured, see below. Intercepts syscalls in
  userspace so untrusted code never talks to the host kernel directly. Cheapest real
  improvement available, and the default in `.env.example`.
- **Rootless Docker** under the broker, as above.
- **Separate the sandbox host.** Move the broker to a second box; the control plane already
  talks to it over HTTP, so this is a URL change plus mTLS. A kernel escape then costs you
  the sandbox host, not the database.
- **Firecracker microVMs.** Real isolation. Implement `SandboxDriver` in
  `app/sandbox/base.py` — nothing else in the codebase changes.

### Installing gVisor

```bash
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt update && apt install -y runsc
runsc install          # registers the runtime in /etc/docker/daemon.json
systemctl restart docker
docker info --format '{{.Runtimes}}'   # must list runsc
```

Then set `SANDBOX_RUNTIME=runsc` in `.env`. Leave `SANDBOX_REQUIRE_RUNTIME=true`: the app
refuses to start if `runsc` is missing, rather than falling back to `runc` and serving
weaker isolation than this document claims.

The cost is real but modest — gVisor adds syscall overhead, so I/O-heavy user code runs
perhaps 10–20% slower. For a platform running strangers' code that is a good trade.

### Beyond gVisor: real VMs, same one-line change

gVisor narrows the shared kernel; it does not remove it. The next step up is a separate
kernel per sandbox, and because `SANDBOX_RUNTIME` is passed straight to Docker, **any OCI
runtime works with no code change**. [Kata Containers](https://katacontainers.io) is the
practical one — it boots a lightweight VM per container and installs as a runtime:

```bash
apt install -y kata-runtime          # or the official installer
kata-runtime check                   # confirms the host supports virtualisation
```

```bash
SANDBOX_RUNTIME=kata
```

Requires nested virtualisation (`/dev/kvm`). Kimsufi is bare metal, so KVM is available —
check with `kvm-ok`. Expect a few hundred milliseconds of extra start-up per sandbox and
noticeably more memory per container, which is why this belongs on the paid tiers first.

The sequence to think in: **gVisor now** (cheap, already configured) → **rootless**
(closes socket-equals-root) → **Kata on paid plans** (removes the shared kernel where the
money justifies the memory) → **separate sandbox host** (contains a kernel escape).

---

## 2. Disks

Four 2 TB drives. Do not use all four as one big stripe: a single drive failure would take
the platform down and lose every workspace.

```bash
# RAID10 across all four: 4 TB usable, survives one drive (often two).
mdadm --create /dev/md0 --level=10 --raid-devices=4 \
      /dev/sda /dev/sdb /dev/sdc /dev/sdd

mkfs.xfs -f /dev/md0
mkdir -p /srv/agentspace
```

Mount with project quotas enabled — this is how per-user disk limits become real rather
than advisory:

```bash
# /etc/fstab
/dev/md0  /srv/agentspace  xfs  defaults,pquota,noatime  0  2
```

```bash
mount -a
```

Application-level accounting in `app/quotas.py` walks the tree and is enough to *reject*
oversized writes, but it cannot stop a runaway process mid-write. XFS project quotas can.
Wire them per workspace directory:

```bash
# One project per user id, capped at the plan's disk_mb.
xfs_quota -x -c "project -s -p /srv/agentspace/workspaces/<user_id> <project_id>" /srv/agentspace
xfs_quota -x -c "limit -p bhard=1g <project_id>" /srv/agentspace
```

Also cap the Docker overlay so container writes cannot fill the root filesystem:

```json
// /etc/docker/daemon.json
{
  "storage-driver": "overlay2",
  "data-root": "/srv/agentspace/docker",
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "live-restore": true,
  "userland-proxy": false
}
```

The log limits matter more than they look: a user service in a crash loop will write
gigabytes of stderr, and unbounded Docker logs are the most common way a small box fills
its disk.

---

## 3. Capacity

32 GB RAM against the free plan's 512 MB ceiling is not 64 users. Budget:

| | |
|---|---|
| Host + Docker + Caddy + Postgres | ~4 GB |
| Control plane (2 uvicorn workers) | ~1 GB |
| Reserve for page cache and spikes | ~4 GB |
| **Available for sandboxes** | **~23 GB** |

Free-tier deployments are long-lived containers holding their memory ceiling. Execution
containers are transient. Two controls decide how far that 23 GB stretches.

**Split the budget.** Executions draw from `EXEC_MEMORY_BUDGET_MB`; services consume the
rest. A sane starting split on this box:

```bash
EXEC_MEMORY_BUDGET_MB=8192        # ~16 concurrent free-tier runs at 512 MB
MAX_CONCURRENT_EXECS_PER_USER=2   # nobody occupies the pool alone
QUEUE_WAIT_SECONDS=15             # then tell the agent to retry
```

That leaves roughly 15 GB for services — about 30 always-on free accounts. Runs beyond the
budget wait briefly and are then refused with a 503 that says to retry, which is a far
better failure than the OOM killer choosing a victim at random.

**Let idle services sleep.** Most free services are visited by nobody:

```bash
SERVICE_IDLE_MINUTES=120          # 0 disables reaping
REAPER_INTERVAL_SECONDS=300
SERVICE_WAKE_TIMEOUT_SECONDS=20
```

A stopped service still answers its URL — the first request restarts it and waits for it
to listen, costing a few seconds of latency once. In practice this is the single largest
capacity win available here: if a third of accounts are active in any two-hour window,
**service capacity roughly triples**, and the ceiling moves from memory to disk.

Tune `SERVICE_IDLE_MINUTES` down if you are memory-bound and users tolerate cold starts;
up if you are not. Setting it to 0 keeps everything resident and reverts you to the ~30
figure above.

The execution limiter keeps its state in Postgres, so it holds across uvicorn workers —
`--workers 2` in the Dockerfile does not double the budget.

---

## 4. DNS

```
A      agentspace.dev        <server-ip>
A      *.agentspace.dev      <server-ip>
AAAA   agentspace.dev        <server-ipv6>
AAAA   *.agentspace.dev      <server-ipv6>
```

The wildcard record is what makes `alice.agentspace.dev` work. Certificates for it need a
DNS-01 challenge, which needs a Caddy build carrying your DNS provider's plugin:

```bash
docker run --rm -v "$PWD:/out" caddy:2-builder \
  xcaddy build --with github.com/caddy-dns/cloudflare --output /out/caddy
```

Then point the `caddy` service at that binary and set `DNS_PROVIDER=cloudflare` plus
`DNS_API_TOKEN` in `.env`.

**You can skip all of this at launch.** Path mode — `agentspace.dev/@alice/` — needs only
the apex certificate and works out of the box. Comment out the `*.{$BASE_DOMAIN}` block in
the `Caddyfile` until wildcard TLS is set up.

Customer-owned domains work through Caddy's on-demand TLS, gated by
`GET /internal/tls-check`, which only answers 200 for hostnames actually claimed by an
active account. Without that gate a stranger's DNS record could exhaust your Let's Encrypt
rate limit.

---

## 5. Install

```bash
apt update && apt install -y docker.io docker-compose-plugin git
systemctl enable --now docker

git clone <your-repo> /srv/agentspace/app
cd /srv/agentspace/app

cp .env.example .env
```

Fill in `.env`:

```bash
BASE_DOMAIN=agentspace.dev
PUBLIC_URL=https://agentspace.dev
ACME_EMAIL=you@example.com
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
POSTGRES_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

# Must be identical on the host and inside the app container: the Docker daemon
# resolves sandbox bind-mount paths on the host, not in the app's namespace.
HOST_DATA_ROOT=/srv/agentspace/workspaces
DATA_ROOT=/srv/agentspace/workspaces

SANDBOX_ALLOW_EGRESS=false
REGISTRATION_OPEN=true
```

`HOST_DATA_ROOT` and `DATA_ROOT` matching is not cosmetic. The app asks the daemon to bind
`<workspace>` into a sandbox; the daemon interprets that path on the host. If they differ,
every sandbox mounts an empty directory and every `run_code` mysteriously sees no files.

Build and start:

```bash
mkdir -p /srv/agentspace/workspaces
docker build -t agentspace/runtime:latest -f docker/runtime.Dockerfile .
docker compose up -d --build
docker compose logs -f app
```

Verify:

```bash
curl -s https://agentspace.dev/health
curl -s https://agentspace.dev/api/v1/hello | head -40
curl -s https://agentspace.dev/.well-known/agent-card.json | head -20
```

---

## 6. Firewall

```bash
ufw default deny incoming
ufw allow 22/tcp
ufw allow 80,443/tcp
ufw allow 443/udp     # HTTP/3
ufw enable
```

The `agentspace_sandbox` network is declared `internal: true` in compose, so user
containers have no route off the host. If you ever set `SANDBOX_ALLOW_EGRESS=true`,
understand that you have just given anonymous strangers a machine to make outbound
connections from — which is to say, an open proxy and an abuse complaint generator. Add an
egress allowlist first.

---

## 6a. Migrations

The schema is owned by Alembic. `docker/entrypoint.sh` runs `alembic upgrade head` once,
before uvicorn forks its workers — putting migrations in the app's startup hook instead
would have every worker racing to apply the same revision.

```bash
alembic current                                    # where this database is
alembic history --verbose                          # what exists
alembic upgrade head                               # apply
alembic downgrade -1                               # step back
alembic revision --autogenerate -m "add widgets"   # after changing a model
```

`alembic revision --autogenerate` compares the models to a database **already at head**,
so upgrade before generating or the diff will be nonsense. Always read the generated file:
autogenerate is good at added tables and columns, and unreliable about renames — it will
happily emit a drop plus an add, which loses the data in that column.

### Adopting a database that predates migrations

An existing database built by the old `create_all` path already matches the baseline
revision, so it is adopted rather than rebuilt:

```bash
cp data/agentspace.db data/agentspace.db.backup   # or pg_dump for Postgres
alembic stamp head        # record that it is already at the baseline
alembic upgrade head      # no-op, confirms the stamp took
```

Verified on a real instance carrying 8 accounts and 2 deployments: row counts unchanged,
and no drift between the models and the adopted schema.

Do **not** run `stamp` on a database that is genuinely behind — it tells Alembic the
migrations already ran without running them, and the next request touching a missing
column fails in exactly the confusing way migrations exist to prevent.

### Two independent checks at boot

Startup logs the revision and complains about two different failures, because they have
different fixes:

- *"DATABASE IS AT x BUT HEAD IS y"* — migrations did not apply. Run `alembic upgrade head`.
- *"MODELS AND DATABASE DISAGREE even at head"* — someone changed a model and never
  generated the migration. Run `alembic revision --autogenerate`.

## 6b. Billing

Stripe, in test mode first. Nothing here touches a card number — checkout and the portal
are hosted by Stripe, which keeps this service out of PCI scope entirely.

```bash
pip install -e ".[billing]"
```

In the Stripe dashboard create one **recurring monthly Price** per paid plan and copy the
`price_...` ids (not the `prod_...` ids):

| Plan | Price |
|---|---|
| Maker | €5 / month |
| Pro | €19 / month |

Then add the webhook endpoint `https://<your-domain>/api/v1/billing/webhook`, subscribed
to these events:

```
checkout.session.completed
customer.subscription.created
customer.subscription.updated
customer.subscription.deleted
invoice.payment_failed
```

Copy its signing secret into `.env`:

```bash
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_MAKER=price_...
STRIPE_PRICE_PRO=price_...
```

Verify the loop end to end before going live:

```bash
stripe listen --forward-to localhost:8000/api/v1/billing/webhook
stripe trigger customer.subscription.created
```

Two behaviours worth knowing because they are deliberate, not accidents:

- **A failed payment does not cut service.** The subscription moves to `past_due` and the
  plan stays. Stripe runs its retry schedule and dunning emails; killing a customer's
  running services on the first missed charge loses the customer you were trying to keep.
  Access ends when Stripe reports `unpaid` or `canceled`.
- **A downgrade does not delete anything.** Someone dropping from Pro to Free keeps their
  existing deployments running but cannot create more until they are under the new limit.
  Deleting a paying-yesterday customer's work is not a limit, it is a punishment.

`STRIPE_SECRET_KEY` empty disables billing entirely and the free tier keeps working, which
is what the test suite and a fresh clone run with.

## 7. Backups

Two things are irreplaceable, and they fail differently:

```bash
# Database: small, changes constantly.
docker compose exec -T postgres pg_dump -U agentspace agentspace \
  | zstd > /backup/db-$(date +%F).sql.zst

# Workspaces: large, mostly static.
rsync -a --delete /srv/agentspace/workspaces/ /backup/workspaces/
```

RAID10 is not a backup — it survives a dead disk, not `rm -rf`, not a bad migration, not a
compromise. Push both off the box. OVH's Backup Storage or any S3-compatible target works;
the point is that it is not this machine.

---

## 8. Costs

| | per month |
|---|---|
| Kimsufi (Xeon, 32 GB, 4×2 TB) | 20 EUR |
| Domain | ~1 EUR |
| Off-box backup (~100 GB) | ~3 EUR |
| **Total** | **~24 EUR** |

Stripe takes roughly 1.5% + €0.25 on European cards, so a €5 Maker nets about €4.42 and a
€19 Pro about €18.46. The box pays for itself at **6 Makers or 2 Pros**. Note that the
fixed fee costs proportionally far more on the cheap plan — €5 is close to the floor where
card fees stop being noise.

At 30–40 active free accounts that is well under 1 EUR per account per month. The first
thing that breaks as you grow is RAM, not disk or bandwidth — 8 TB raw against 1 GB
workspaces means disk is a non-issue for a long time. Plan the second box as a sandbox
host, not as more storage.

---

## 8b. Running a closed beta

Everything an operator does runs from one command, over SSH. There is no admin web
page on purpose: an admin UI needs an admin session, an admin role and an admin login
form — three new ways into the system, guarding something one person looks at a few
times a day.

```bash
docker compose exec app python -m app.cli status
```

```
schema revision  153b1f097d11
users            8 (free=8), 0 suspended
deployments      idle=1, live=1
sandbox pool     0 running, 0/8192 MB
compute (24h)    153.5s
workspaces       13.4 KB
invites unused   3
registration     invite only
```

### Letting people in

```bash
INVITE_REQUIRED=true          # in .env, then restart
```

```bash
docker compose exec app python -m app.cli invite create --count 10 \
  --note "hn thread" --expires-days 30
docker compose exec app python -m app.cli invite list
```

Codes look like `LB4U-7RRJ-QYW3` — no `I`, `O`, `0` or `1`, because they get read aloud
and typed by hand. Each works once, and a registration that fails for another reason
(username taken, say) does **not** consume the code.

### Day to day

```bash
python -m app.cli user list
python -m app.cli user suspend someone --reason "running a crypto miner"
python -m app.cli user restore someone
python -m app.cli user password someone     # prints a new one; no email yet
python -m app.cli user plan someone pro     # comp an account
python -m app.cli user delete someone       # irreversible, prompts first
```

Two things worth knowing:

- **`--reason` is mandatory on suspend**, and it is shown to the account. A suspended
  key fails with `account_suspended` and the reason, not `invalid_api_key` — an agent
  told its key is bad will hunt a credential problem that does not exist.
- **`user plan` on a real Stripe subscriber gets overwritten** by the next webhook. The
  command warns when that applies; change the subscription in Stripe instead.

### Capacity for 10–30 testers

Comfortable. Free services sleep after an hour idle and wake on the next request, so
thirty testers use far fewer than thirty slots — testers do not leave services running
overnight. Watch `sandbox pool` in `status`: if `used_mb` sits near `budget_mb`, raise
`EXEC_MEMORY_BUDGET_MB` or lower `SERVICE_IDLE_MINUTES`.

## 9. Operating notes

```bash
# What is running, and for whom
docker ps --filter label=agentspace.kind=service \
          --format 'table {{.Names}}\t{{.Status}}\t{{.Label "agentspace.user"}}'

# Clean up exec containers that outlived their request
docker container prune --filter label=agentspace.kind=exec --filter until=1h -f

# Per-container resource use
docker stats --no-stream --filter label=agentspace.kind=service
```

Set up log rotation and a disk-usage alert before you need them. The failure mode of a
full disk on this design is Postgres refusing writes, which looks like the whole platform
being down.
