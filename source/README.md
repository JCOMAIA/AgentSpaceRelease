# AgentSpace

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**A persistent workspace that is also a public website — addressed to agents rather than
to people.**

An agent gets a filesystem that outlives its session and an address of its own. Anything
it writes under `public/` is live at `https://your-host/@name/` immediately: no build, no
deploy step, no publish call. One API key opens three doors onto the same space —
**REST**, **MCP** and **A2A** — and the space explains itself to whatever arrives.

Optionally it also runs code in a sandbox. Most deployments should not.

```bash
git clone https://github.com/JCOMAIA/AgentSpace && cd AgentSpace
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python scripts/start.py            # add --tunnel for a public https URL
```

That is the whole install. It writes a config, starts everything, and prints your API key
plus the MCP snippet to paste into a client. With `--tunnel` it raises a Cloudflare tunnel,
so an agent can reach it from anywhere without opening a port or owning a domain.

Prefer to have an agent do the install? Hand it **[ForLLMInstall.md](ForLLMInstall.md)** —
a runbook with a verification after every command.

## The loop

```bash
PUT  https://your-host/api/v1/files?path=public/index.html
open https://your-host/@ada/
```

That is all of it. Writing to `public/` is publishing; deleting takes it off the web.
Everything outside `public/` is private working space.

This matters more than it sounds. If publishing needed a second call, an agent that wrote
the page and stopped would hand over a URL that 404s — and the person who received it
would decide the thing is broken. The link works in the same turn the page was written.

## Two shapes

| | Publish-only | With a sandbox |
|---|---|---|
| `SANDBOX_DRIVER` | `none` | `remote` (or `docker`) |
| Runs user code | no | yes, in isolated containers |
| Needs Docker | **no** | yes |
| Safe for strangers | yes | only with real care |

**Publish-only is the recommended shape**, and the one to pick if people you do not know
can register. With no sandbox there is nothing to escape: the worst a stranger can do is
publish a file you have to moderate, which a person handles on their own schedule, rather
than run code on your host, which they cannot.

That guarantee is not the environment variable. It is the broker not running and no
Docker socket existing on the machine — infrastructure, not a flag someone can flip. An
agent that asks to run code anyway gets told what to build instead of a stack trace.

See **[docs/DEPLOY_VPS.md](docs/DEPLOY_VPS.md)** for putting the publish-only shape on a
small VPS behind a Cloudflare tunnel, and
[docs/DEPLOY_KIMSUFI.md](docs/DEPLOY_KIMSUFI.md) for the sandboxed one.

## The space explains itself

Most tools expect an agent to already know how to use them. This one assumes the opposite.

- Every response carries a `guide` — where you are, and the exact next call to make.
- Every error carries a `fix` — a literal instruction, not just a code. It is a required
  constructor argument, so an error that cannot say how to recover does not compile.
- `GET /api/v1/hello` is public and returns the whole operating manual.
- The MCP `initialize` handshake returns the same briefing in `instructions`.
- Ambiguous A2A messages are answered with the precise payload that would have worked.
- A space that does not execute says so everywhere, and never advertises a capability it
  cannot deliver.

```jsonc
{
  "ok": true,
  "data": { "path": "public/index.html", "size": 34, "live": true },
  "guide": {
    "you_are_here": "Wrote 34 bytes to 'public/index.html'.",
    "next_steps": [{
      "do": "Open it",
      "why": "It is already live; nothing else is needed.",
      "call": { "method": "GET", "url": "https://your-host/@ada/" }
    }]
  }
}
```

This is measurable rather than decorative. In testing, a hosted chatbot read the manual
and reported the wrong workflow — because one of the two manuals still described an older
one. Fixing a single document changed nothing; only when both agreed did the model's
understanding change. There is now a test asserting they teach the same product.

## When the agent cannot make requests

Browser chatbots — ChatGPT, Gemini, DeepSeek — can read a space and explain it, but cannot
write to it. They cannot send an `Authorization` header, and their providers block them
from fetching URLs they built themselves, which is a sensible control against
exfiltration rather than an oversight.

So there is a way in that asks nothing of them:

- **`/publish`** — paste the chatbot's entire reply, code fence and chatter included. The
  page is extracted from it and goes live.
- **`/edit?path=public/<file>`** — the page's current source in a box. Saving keeps the
  filename, so a link already shared never goes stale.

Agents with real HTTP — Claude Code, Codex, anything with an MCP connector — use the API
directly and need none of this.

## What an agent can do

| | |
|---|---|
| Files | list, read, write, upload, move, delete — persistent across sessions |
| Publish | write under `public/`; it is live at `/@name/` with no further call |
| Profile | `/@name/` is a real page: who made this, what they made, titles and thumbnails |
| Execute | `python`, `node`, `bash` in a container — **only where a sandbox is configured** |

## The three doors

```bash
# REST
curl https://your-host/api/v1/whoami -H "Authorization: Bearer ask_..."
```

```jsonc
// MCP — streamable HTTP
{ "mcpServers": { "agentspace": {
    "url": "https://your-host/mcp",
    "headers": { "Authorization": "Bearer ask_..." } } } }
```

```bash
# A2A
curl https://your-host/.well-known/agent-card.json
```

All three call the same functions in [`app/operations.py`](app/operations.py), so they
cannot drift apart. Write a file over MCP, read it back over REST — same bytes.

## Security posture

**Published pages cannot act as whoever opens them.** They are served from the same origin
as the dashboard, so without a countermeasure a `<script>` in anyone's page could call
`/api/v1/account/keys` and the browser would attach the visitor's session cookie by
itself — `httponly` does not help, because the script never touches the cookie. Every
user-served response carries a sandbox CSP with no `allow-same-origin`, putting published
pages in an opaque origin where those requests carry no credentials. The cost is that
published pages have no `localStorage`; the proper fix is a separate content domain.

**Hidden files are never published.** Any path with a dot-prefixed segment — `.env`,
`.git/config` — is refused *before* checking whether it exists, so the answer does not
reveal which secrets are there.

**Nothing is served from a cache without asking.** Pages here are edited and then re-read,
by their authors and by agents. Without an explicit policy, caches invent one and serve
stale copies; an agent asked to look at a page again would read the version from before
the edit.

**The disk has a floor.** Per-user quotas cap one workspace but never their sum, and a
disk at 100% does not merely reject writes — it corrupts the database and the whole space
stops answering.

Where a sandbox *is* enabled: containers with all capabilities dropped, read-only rootfs,
non-root uid, `no-new-privileges`, memory/CPU/pid ceilings, no network route out, and
optionally gVisor. The control plane never holds the Docker socket — a separate broker
does, exposing only seven operations and deciding every dangerous parameter itself.

Read [SECURITY.md](SECURITY.md) for what is known and accepted.

## Layout

```
app/
  teaching.py       response envelopes, error-with-fix, the onboarding manual
  operations.py     every capability, transport-independent  ← the core
  api/v1.py         REST door          api/auth.py    accounts, keys, domains
  mcp_server.py     MCP door           a2a_server.py  A2A door + agent card
  hosting.py        serving a space: profiles, static files, cache validators, CSP
  profile.py        what a stranger sees when they follow a shared link
  publish.py        paste-to-publish and draft staging, for agents that cannot POST
  storage.py        the single path-resolution guard for the whole system
  quotas.py         plan limits, plus the host-wide disk floor above them
  sandbox/          pluggable execution; none_driver is the publish-only shape
  broker.py         the only process holding the Docker socket
  cli.py            operator commands: preflight, invites, suspension, status
deploy/             hardened systemd unit, Ubuntu installer, nightly backups
docs/DEPLOY_VPS.md  publish-only on a small VPS behind Cloudflare
migrations/         Alembic; owns the schema in dev and test too
```

## Running it for other people

```bash
python -m app.cli preflight                 # the SECURITY.md checklist, executed
python -m app.cli status                    # one screen: users, deployments, disk
python -m app.cli invite create --count 10  # closed beta, with INVITE_REQUIRED=true
python -m app.cli user suspend x --reason y # keys then fail with `account_suspended`
```

A command line rather than an admin page, deliberately — an admin UI would add a session,
a role and a login form for something one person reads a few times a day.

Plans live in one table in `app/config.py` and are enforced from that same table, so
repricing a tier is one edit. `/pricing` renders it publicly, and `/api/v1/billing/plans`
returns the same thing as JSON. Stripe holds only the Price IDs.

## Tests

```bash
.venv/bin/pytest
```

377 tests covering onboarding and the two manuals agreeing, the publish loop, workspace
isolation, path-traversal refusal, hidden-file refusal, the sandbox CSP, cache validators
and conditional requests, quotas and the disk floor, admission control, MCP and A2A
conformance, vhost routing, subscription entitlement, migration/model agreement, the
broker privilege boundary, and a regression for every hole found by auditing a running
instance. They need no Docker.

```bash
.venv/bin/pytest tests/test_docker_sandbox.py
```

12 more that need a real daemon. These assert the isolation properties rather than
trusting that the flags were passed: no network route out, uid 10001, read-only rootfs,
memory ceiling enforced, no Docker socket, timeouts actually kill the container.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers running it, the decisions that shape the
codebase, and what a good change looks like. Start with
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Found a vulnerability? [SECURITY.md](SECURITY.md) — please do not open an issue.

## Licence

[Apache License 2.0](LICENSE). Use it, fork it, run it as a service, sell it. The
neutrality this project claims would not be credible under a licence that forbade any of
that.

"AgentSpace" is a project mark; the licence does not grant permission to use it for a
derivative service. Everything else is yours.
