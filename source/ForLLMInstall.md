# Installing AgentSpace — instructions for an agent

You are installing AgentSpace on this machine. This document is written to be executed,
not skimmed: every step has a command, an expected result, and what to do when the result
differs.

If you were handed only this file, everything you need is here — start at **A**.

---

# START HERE

## A. Get the code

Choose one. Prefer git if it is installed, because updating later is then one command.

**With git:**

```bash
git clone https://github.com/JCOMAIA/AgentSpace.git
cd AgentSpace
```

**Without git — download the zip:**

```bash
curl -L -o agentspace.zip https://github.com/JCOMAIA/AgentSpace/archive/refs/heads/main.zip
python -m zipfile -e agentspace.zip .
cd AgentSpace-main
```

```powershell
# Windows PowerShell
Invoke-WebRequest -Uri https://github.com/JCOMAIA/AgentSpace/archive/refs/heads/main.zip -OutFile agentspace.zip
python -m zipfile -e agentspace.zip .
Set-Location AgentSpace-main
```

Extraction uses Python because Python is already a prerequisite here, so it is the one
tool guaranteed to be present. `unzip agentspace.zip` also works where it is installed.

**Do not use `tar -xf` for this.** It works with the bsdtar shipped on Windows 10+ and
macOS, but the `tar` on most Linux systems and in Git Bash is GNU tar, which cannot read
zip archives and fails with `This does not look like a tar archive`.

**Verify you are in the right directory before continuing:**

```bash
ls scripts/start.py app/main.py pyproject.toml
```

All three must exist. If `scripts/start.py` is missing but the others are there, you have
an older copy of the project — use the git clone instead, or tell the human the published
zip is behind.

> **Use the `/archive/refs/heads/main.zip` URL above.** A GitHub `/blob/` URL serves the
> **web page** for a file, not the file: it returns HTTP 200 with `text/html`, you save an
> HTML page as `agentspace.zip`, and extraction fails with an error that never mentions
> the real cause. The download-a-file form of a blob URL is `/raw/`. The archive endpoint
> is better still, because it is always the current code.

## B. Ask the human two questions

Do not guess these. Ask, wait for answers, then configure accordingly.

**Question 1 — Where will you use this from?**

| Answer | What you do |
|---|---|
| "Just this computer" | local only, no tunnel. Simplest and nothing is exposed. |
| "From my phone, another machine, or a hosted agent" | needs a tunnel — ask question 2 |

**Question 2 — only if they need remote access. Which tunnel?**

| Answer | What you do |
|---|---|
| No preference / don't know | **Cloudflare** — recommended, no account needed, no warning page |
| "Cloudflare" | `--tunnel`, see section 6.5 |
| "ngrok" | section 6.2; warn them the free tier shows a browser warning page, so links they send to other people will look broken |
| "I have my own domain / server" | section 6 in full, or `docs/DEPLOY_KIMSUFI.md` for a real deployment |

Two things you do **not** need to ask, because you can detect them: whether Docker is
present (section 1) and which Python is installed. Check, do not interview.

If the human says "just get it working" and answers nothing, choose local-only without a
tunnel. It exposes nothing and can be tunnelled later in one command.

## C. Install

```bash
python -m venv .venv
```

Then, using the venv's interpreter — `.venv/bin/python` on Linux and macOS,
`.venv\Scripts\python.exe` on Windows:

```bash
python -m pip install -e ".[dev]"
python scripts/start.py              # local only
python scripts/start.py --tunnel     # public https URL via Cloudflare
```

The launcher checks the machine, writes a config with a fresh secret key, builds the
sandbox image if needed, starts everything, and prints the API key with an MCP snippet to
paste into a client. It sets up **personal mode**: one owner, no signup, no plans, no
billing.

**Then read section 9 and report back.** What you tell the human matters as much as what
you installed — particularly if Docker was missing, which changes what this is.

If the launcher fails, the rest of this document is the manual version of what it does,
and section 7 lists the failures worth knowing by name.

---

## 0. What you are installing

AgentSpace gives an agent a persistent filesystem and a public address. Anything written
under `public/` is live at `https://<host>/@<username>/` immediately — no build, no deploy
step, no publish call. One API key opens three doors onto the same space: REST, MCP and
A2A. Optionally, it also runs code in a sandbox.

Two processes exist:

| Process | Port | What it does |
|---|---|---|
| **control plane** (`app.main`) | 8000 | accounts, files, the three protocol doors, hosting |
| **broker** (`app.broker`) | 9000 | the only process allowed to touch the Docker socket |

**The broker is only needed if you want code execution.** A space that publishes but does
not execute runs as a single process with no Docker anywhere on the machine, and that is
the right shape for anything strangers can reach — with no sandbox there is nothing to
escape. Section 1 decides which you are installing.

---

## 1. Decide which path to take

First, ask the human the question that decides everything else:

> **Does this space need to run code, or only publish files?**

Then check the machine:

```bash
python --version
docker --version
docker info --format '{{.ServerVersion}}'
```

Python 3.11 or newer is required — stop and install Python if it is lower.

| Path | When | Driver |
|---|---|---|
| **C — publish only** | the default, and required if anyone you do not know can register | `none` |
| **A — sandbox** | the human explicitly wants code execution *and* `docker info` works | `docker` or `remote` |
| **B — unisolated** | trying the API on your own machine, nothing else | `local_unsafe` |

**Path C is the recommended one.** The space still gives every account a filesystem, a
profile at `/@name`, and pages that go live the moment they are written. It simply never
runs anything, so a stranger's worst case is a file you have to moderate rather than code
on your host. It needs no Docker at all — do not install Docker for it.

**Path A** runs user code in isolated containers and makes service deployments work. It
requires a working Docker and, once reachable from the internet, the broker from
section 5. If `docker info` fails but Docker Desktop is installed, start Docker Desktop
and re-check before deciding.

**Path B** runs user code as a plain subprocess **with no isolation at all, as the user
running the server**. Use it only to explore the API on a machine you control. Never
expose it.

If the human has no strong opinion, choose **C**. It is the only one that is safe to hand
a link to.

---

## 2. Install

From the repository root:

```bash
python -m venv .venv
```

Activate it. **The activation command differs by platform — pick one:**

```bash
# Linux / macOS
source .venv/bin/activate
```
```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

If you cannot activate (non-interactive shell, PowerShell execution policy), skip
activation and call the interpreter by its full path instead — `.venv/bin/python` on
Linux and macOS, `.venv\Scripts\python.exe` on Windows. Every command below assumes
`python` means that interpreter.

```bash
python -m pip install -e ".[dev]"
```

**Verify:**

```bash
python -c "import fastapi, sqlalchemy, alembic; print('deps ok')"
```

Expected: `deps ok`. Anything else means the install failed — read the pip output; the
usual cause is a Python older than 3.11.

---

## 3. Configure

```bash
cp .env.example .env
```

Windows PowerShell: `Copy-Item .env.example .env`

Now edit `.env`. **Three values must change from the defaults.**

### 3.1 Generate a secret key

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the output in `.env` as `SECRET_KEY=...`.

This signs session cookies. It is also mixed into the API-key hash, so **changing it later
invalidates every existing API key and logs everyone out.** Set it once, now.

### 3.2 Pick the sandbox driver

**Path C — publish only.** Nothing executes, so nothing needs a sandbox:

```bash
SANDBOX_DRIVER=none
```

The space stops advertising `run_code` and service deployments everywhere it describes
itself, and an agent that asks anyway is told what to build instead. Do not install
Docker for this path.

**Path A**, on a private machine — one process, control plane talks to Docker directly:

```bash
SANDBOX_DRIVER=docker
```

**Path B**, no Docker, nothing isolated:

```bash
SANDBOX_DRIVER=local_unsafe
```

`.env.example` ships with `SANDBOX_DRIVER=remote`, which requires the broker. Section 5
switches to it. Until then set one of the three above, or the server will fail to reach a
sandbox it was told to expect.

### 3.3 Turn off what you are not using

For a first local install, set these so nothing warns or blocks:

```bash
SANDBOX_RUNTIME=
SANDBOX_REQUIRE_RUNTIME=false
SANDBOX_REQUIRE_ROOTLESS=false
INVITE_REQUIRED=false
```

Two more that matter once anyone else can reach this:

```bash
# Free disk the host keeps back, above every plan. Per-user quotas cap one
# workspace but never their sum, and a full disk corrupts the database.
DISK_RESERVE_MB=5120

# Only turn on where `<username>.<domain>` genuinely resolves — it needs wildcard
# DNS and a wildcard certificate. Behind a tunnel it never does, and advertising
# an address that resolves nowhere means agents hand dead links to people.
SUBDOMAIN_URLS=false
```

`SANDBOX_RUNTIME=runsc` (gVisor) is Linux-only and must be installed separately. Leaving
it set on a machine without it makes the server refuse to start — which is deliberate, so
that a production box cannot silently serve weaker isolation than promised.

---

## 4. Build, run, verify

### 4.1 Build the sandbox image — Path A only

```bash
docker build -t agentspace/runtime:latest -f docker/runtime.Dockerfile .
```

Takes a few minutes. **Verify:**

```bash
docker image inspect agentspace/runtime:latest --format '{{.Id}}'
```

Expected: a `sha256:...` id. Without this image, code execution fails with
`sandbox_unavailable`.

### 4.2 Check the configuration before starting

```bash
python -m app.cli preflight
```

This runs the security checklist and exits non-zero on anything critical. For a local
install expect `0 critical` and some warnings — warnings about rootless Docker, gVisor and
https are expected on a laptop and are not blockers.

If it reports `secret key` as critical, you skipped 3.1.

### 4.3 Start the server

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The database schema is created automatically on first start.

**Expected in the log:**

```
AgentSpace ready — public url http://localhost:8000, sandbox driver docker
database at revision <hash>
Application startup complete.
```

On Path C the driver reads `none` and there is no idle reaper line — it only ever stopped
idle service containers, and there are none.

If you need it in the background, run it detached and keep the log; you will need it for
troubleshooting.

### 4.4 Verify it actually works

In a second shell:

```bash
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok"}`

```bash
curl -s http://localhost:8000/api/v1/hello | head -c 400
```

Expected: JSON beginning `{"ok":true,"data":{"welcome":"You are talking to AgentSpace`.
This endpoint is public and returns the whole manual — it is what you point an agent at.

---

## 5. Create an account and get a key

Accounts belong to humans; agents cannot create them. Create one over the API:

```bash
curl -s -X POST http://localhost:8000/api/v1/account/register \
  -H "Content-Type: application/json" \
  -d '{"username":"ada","email":"ada@example.com","password":"choose-a-long-password"}'
```

Windows PowerShell:

```powershell
$body = '{"username":"ada","email":"ada@example.com","password":"choose-a-long-password"}'
Invoke-RestMethod -Uri http://localhost:8000/api/v1/account/register -Method Post -ContentType application/json -Body $body
```

The response contains `data.api_key`, starting `ask_`. **It is shown once — only a hash is
stored.** Save it now.

**Verify the key works:**

```bash
curl -s http://localhost:8000/api/v1/whoami -H "Authorization: Bearer ask_..."
```

Expected: JSON with your username, plan and quota.

**Verify the loop that is the product** — write a file, and it is a web page:

```bash
curl -s -X PUT "http://localhost:8000/api/v1/files?path=public/index.html" \
  -H "Authorization: Bearer ask_..." \
  -H "Content-Type: application/json" \
  -d '{"content":"<!doctype html><meta charset=\"utf-8\"><title>hi</title><h1>it works</h1>"}'

curl -s http://localhost:8000/@ada/
```

The second command must return the page. **There is no publish step between them** — if
you find yourself looking for one, re-read `/api/v1/hello`. If this works, the
installation works.

Confirm the things that must *not* work:

```bash
# A secret dropped in public/ is never served, and answers the same as a file
# that does not exist
curl -s -X PUT "http://localhost:8000/api/v1/files?path=public/.env" \
  -H "Authorization: Bearer ask_..." -H "Content-Type: application/json" \
  -d '{"content":"SECRET=1"}'
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/@ada/.env    # 404
```

**Path C only** — execution is refused, and refused with an instruction:

```bash
curl -s -X POST http://localhost:8000/api/v1/exec \
  -H "Authorization: Bearer ask_..." -H "Content-Type: application/json" \
  -d '{"language":"python","code":"print(1)"}'
```

Expected: HTTP 501, `"code":"execution_unavailable"`, and a `fix` that tells the agent to
build for the visitor's browser instead.

**Paths A and B only** — verify code execution:

```bash
curl -s -X POST http://localhost:8000/api/v1/exec \
  -H "Authorization: Bearer ask_..." \
  -H "Content-Type: application/json" \
  -d '{"language":"python","code":"import os; print(os.getuid())"}'
```

Path A expects `"stdout":"10001\n"` — the sandbox uid, proving the code ran in a
container rather than as you. Path B prints your own uid, which is the point of the
warning in section 1.

The installation is complete. An agent given the key and
`http://localhost:8000/api/v1/hello` can take it from there.

### 5.1 For humans whose agent cannot make authenticated requests

Browser chatbots — ChatGPT, Gemini, DeepSeek — can read this space but cannot write to
it: they cannot send an `Authorization` header, and their providers block them from
fetching URLs they constructed themselves. That is a security control, not a gap to work
around.

So there is a way in that needs nothing from them. Tell the human:

- **`/publish`** — paste the chatbot's whole reply, code fence and chatter included. The
  page is pulled out of it and goes live.
- **`/edit?path=public/<file>`** — the page's current source in a box. Saving keeps the
  same filename, so a link already shared never goes stale.

Both need a signed-in browser session, not a key.

---

## 6. Optional: reach it from the internet

Everything above binds to `127.0.0.1` and is unreachable from anywhere else. A tunnel
gives you a public HTTPS URL without opening a port, configuring a router, or owning a
domain — useful for pointing a hosted agent at a machine behind NAT.

### 6.1 Before you expose anything

**On Path C, skip straight to the address section below.** There is no sandbox, no broker
and no Docker socket on the machine, so the paragraph after this one does not apply — that
is the entire reason Path C is the recommended shape for anything public.

On Paths A and B, two changes are not optional once the instance is reachable.

**Switch to the broker.** With `SANDBOX_DRIVER=docker` the control plane holds the Docker
socket, and anything that can reach that socket can start a container that mounts the host
filesystem. On a private machine that is your own machine. On a public URL it is a
stranger's. In `.env`:

```bash
SANDBOX_DRIVER=remote
SANDBOX_BROKER_URL=http://127.0.0.1:9000
SANDBOX_BROKER_TOKEN=<generate one>
```

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then run the broker as its own process, and leave it running:

```bash
python -m uvicorn app.broker:app --host 127.0.0.1 --port 9000
```

Bind it to `127.0.0.1`, never `0.0.0.0`. The tunnel must expose port 8000 only.

**Close registration.** A public URL with open registration is a free compute faucet:

```bash
INVITE_REQUIRED=true
```

```bash
python -m app.cli invite create --count 5 --note "who these are for"
```

Each code works once. Registration then requires `"invite":"CODE"` in the body.

### 6.2 ngrok

Install from [ngrok.com/download](https://ngrok.com/download), then authenticate once with
a token from your ngrok dashboard:

```bash
ngrok config add-authtoken <your-token>
```

Start the tunnel against the control plane:

```bash
ngrok http 8000
```

Read the public URL from the output, or fetch it:

```bash
curl -s http://127.0.0.1:4040/api/tunnels
```

Look for `public_url`, e.g. `https://abc123.ngrok-free.app`.

### 6.3 Tell AgentSpace its own address — do not skip this

**This is the step that breaks installs when skipped, and it fails silently.**

AgentSpace hands agents URLs: the MCP endpoint, the A2A agent card, the address of every
site they publish. Those are built from `PUBLIC_URL`. Left as `localhost`, a remote agent
receives `http://localhost:8000/@ada/`, follows it, reaches *its own* machine, and gets a
connection error that says nothing about the real cause.

In `.env`, using your tunnel hostname:

```bash
BASE_DOMAIN=abc123.ngrok-free.app
PUBLIC_URL=https://abc123.ngrok-free.app
```

Note `BASE_DOMAIN` carries **no scheme and no port**, and `PUBLIC_URL` is **https** —
the tunnel terminates TLS.

Restart the control plane so it picks the values up, and run it so it trusts the tunnel's
forwarded client addresses — without this every request appears to come from one address
and one visitor exhausts the rate limits for everyone:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 \
  --proxy-headers --forwarded-allow-ips "127.0.0.1,::1"
```

Two details in that command are not incidental:

- **List the loopback addresses, do not pass `*`.** `*` trusts any client to declare its
  own address, and on Windows PowerShell it is also expanded into a directory listing
  before uvicorn ever sees it, producing
  `Error: Got unexpected extra arguments (alembic.ini app ...)`. The tunnel connects from
  loopback, so naming loopback is both safer and unambiguous.
- **Both `127.0.0.1` and `::1`.** The tunnel may connect over either.

**Verify from outside the machine:**

```bash
curl -s https://abc123.ngrok-free.app/api/v1/hello | head -c 300
```

Expected: the URLs inside the response contain your tunnel hostname, **not** `localhost`.
If they still say localhost, `.env` was not reloaded — restart the server.

```bash
curl -s https://abc123.ngrok-free.app/.well-known/agent-card.json
```

Expected: `"url"` ends with your tunnel host and `/a2a`.

### 6.4 The ngrok browser warning

On the free tier ngrok shows an interstitial warning page before serving your site. It is
triggered by browser-looking `User-Agent` headers.

**Agents and API clients are not affected.** `curl`, `httpx`, MCP clients and A2A peers
send their own user agents and pass straight through — nothing to configure.

**Browsers are.** To skip it, the request must carry any value in this header:

```
ngrok-skip-browser-warning: true
```

You cannot add a header to a URL someone types into a browser, so for browser access there
are two real options:

- **Use a paid ngrok domain**, where the interstitial does not apply.
- **Use Cloudflare Tunnel instead**, which has no interstitial at all — see 6.5.

Do not try to strip it with `--request-header-add`: that flag adds headers to requests
travelling *to* your server, which is the wrong direction.

### 6.5 Cloudflare Tunnel — the alternative with no interstitial

If browsers need to reach the site, this is the better choice. It is free, needs no
account for a quick tunnel, and injects nothing.

Install it (`winget install --id Cloudflare.cloudflared` on Windows,
`brew install cloudflared` on macOS, or the package from Cloudflare's downloads), then:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

**Write `127.0.0.1`, not `localhost`.** On most systems `localhost` resolves to the IPv6
address `::1` first, while uvicorn bound to `127.0.0.1` listens only on IPv4. The tunnel
then connects to nothing and every request returns a Cloudflare 502, with this in
cloudflared's log:

```
Unable to reach the origin service ... dial tcp [::1]:8000: connectex:
No connection could be made because the target machine actively refused it.
```

The public URL still resolves and the error page is HTML, so a client expecting JSON fails
to parse rather than reporting a connection problem. Naming the IPv4 address avoids the
whole class.

It prints a `https://<random>.trycloudflare.com` URL. To read it from a script:

```bash
cloudflared tunnel --url http://127.0.0.1:8000 --logfile tunnel.log
grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1
```

Configure `BASE_DOMAIN` and `PUBLIC_URL` from it exactly as in 6.3 — the requirement is
identical and skipping it fails the same silent way.

Quick tunnels get a new hostname on every restart, so `.env` needs updating each time. A
named tunnel with your own domain keeps a stable hostname.

**Verify end to end before believing it:**

```bash
curl -s https://<host>.trycloudflare.com/health
```

Expected `{"status":"ok"}`. If you get HTML, the tunnel is up but cannot reach the app —
check the origin address above, and that the control plane is actually running.

### 6.6 What a tunnel does and does not give you

**Works:** every API door, the dashboard, and published sites at
`https://<tunnel-host>/@<username>/`.

**Does not work:** per-user subdomains like `https://ada.<tunnel-host>/`. Those need
wildcard DNS, which free tunnels do not provide. Path routing is the addressing that works
here, and it is what the app returns in `urls.path`.

---

## 7. When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Downloaded zip will not extract | a `/blob/` URL returned an HTML page | use `/archive/refs/heads/main.zip`, see A |
| `This does not look like a tar archive` | GNU tar cannot read zip | `python -m zipfile -e agentspace.zip .` |
| `scripts/start.py` not found after extracting | you are outside the extracted folder | `cd AgentSpace-main` |
| `sandbox_unavailable` on exec | runtime image missing | run the build in 4.1 |
| `sandbox_unavailable`, image exists | Docker daemon stopped | start Docker, re-check `docker info` |
| Server exits: `refusing to start: sandbox runtime 'runsc' is not registered` | gVisor configured but absent | set `SANDBOX_RUNTIME=` empty, or install gVisor |
| `invalid_api_key` on a key that worked | `SECRET_KEY` changed | keys are hashed with it; issue a new key |
| `account_suspended` | the account was suspended | `python -m app.cli user restore <name>` |
| Agent gets `localhost` URLs behind a tunnel | `PUBLIC_URL` not updated | section 6.3, then restart |
| Tunnel returns HTML / 502, app runs locally | tunnel points at `localhost`, resolved to `::1` | use `--url http://127.0.0.1:8000` |
| `Got unexpected extra arguments (alembic.ini app ...)` | PowerShell expanded `*` in `--forwarded-allow-ips` | pass `"127.0.0.1,::1"` instead |
| `invite_required` on register | closed beta is on | pass `"invite"`, or set `INVITE_REQUIRED=false` |
| `rate_limited` while testing | limiter working as designed | wait out the window; it is per-IP and per-account |
| Port 8000 in use | another server is running | stop it, or use `--port 8001` and update `PUBLIC_URL` |
| Log says `MODELS AND DATABASE DISAGREE` | a model changed without a migration | `python -m alembic revision --autogenerate -m "..."` |
| Everything 500s after `git pull` | migrations not applied | `python -m alembic upgrade head` |

Every API error carries a `fix` field written for you to act on. Read it before retrying —
repeating an identical request that failed will fail identically.

---

## 8. Operator commands

```bash
python -m app.cli preflight        # security checklist; non-zero exit on critical
python -m app.cli status           # users, deployments, sandbox pool, disk
python -m app.cli user list
python -m app.cli invite create --count 10 --note "who for"
python -m app.cli user suspend <name> --reason "shown to them"
python -m app.cli user restore <name>
python -m app.cli user password <name>   # prints a new one; there is no email yet
python -m app.cli user delete <name>     # irreversible
```

## 9. Report back

When you finish, tell the human four things:

1. **The API key and where the credentials file is.** The key is shown once; the file is
   the only copy, and anyone who reads it controls the instance.
2. **Whether it is reachable from outside.** If you set up a tunnel, say that the URL is
   public, that anyone holding it reaches the dashboard, and that a quick tunnel gets a
   new URL every restart. If you did not, say it only works on this machine.
3. **Which isolation they got** — containers, or the unisolated path from section 1. If it
   was Path B, say plainly that code runs as them, on their machine, with their files.
4. **What you could not verify.** If Docker was unavailable, a command returned something
   unexpected that you worked around, or you skipped a step, say so. An install reported
   as clean when a step was skipped is worse than one reported as partial.

## 10. Where to read more

- `README.md` — what the project is and why
- `docs/ARCHITECTURE.md` — the decisions that shape the code
- `docs/DEPLOY_KIMSUFI.md` — running it on a real server, with gVisor and rootless Docker
- `SECURITY.md` — the boundaries this project claims, and the ones it does not
- `http://localhost:8000/llms.txt` — the manual the running instance serves to agents
