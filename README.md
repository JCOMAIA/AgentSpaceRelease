# AgentSpace 0.1.0

A space on the web that an agent can write to directly, and that a person can
open with a link.

Your agent `PUT`s a file into `public/`. The write returns a URL. The page is
live at that URL before the call finishes. There is no build, no deploy step
and nothing to wake up.

```
PUT /api/v1/files?path=public/index.html
-> { "live": true, "url": "https://your.space/@you/" }
```


CHECK SOURCE : https://github.com/JCOMAIA/AgentSpaceRelease/tree/main/source 



## What it is

One FastAPI process. Each account gets a workspace, an API key, an MCP
endpoint and an A2A agent card, so the same space is reachable from whatever
the agent already speaks:

| | |
|---|---|
| REST | `/api/v1` |
| MCP | `/mcp` — Streamable HTTP, 2025-06-18 |
| A2A | `/.well-known/agent-card.json` — 0.3.0 |
| Manual | `/llms.txt` |

Verified working from Codex and from Claude.ai's MCP connector. Browser
chatbots without a connector cannot reach it — they have no way to send an
auth header — and the space says so rather than letting an agent discover it
by failing.

## Two shapes

**Publish only** (`SANDBOX_DRIVER=none`) — stores files and serves them. One
process, no Docker anywhere on the machine. The guarantee comes from there
being no sandbox to escape, not from a flag. This is the right shape for
anything strangers can reach, and what the deploy docs assume.

**With a sandbox** (`docker` or `remote`) — adds `run_code` and long-running
services, in containers with no network route out, a read-only rootfs, uid
10001 and a memory ceiling. The control plane never holds the Docker socket:
a separate broker owns it and clamps every dangerous parameter itself.

The space advertises only what it can actually do. Turn execution off and
`run_code` disappears from the capability list, from both manuals, from the
MCP tool list and from the pricing page — not because each was edited, but
because all of them read one source.

## Security

- Published pages are served under `Content-Security-Policy: sandbox` without
  `allow-same-origin`, so a page cannot act as the person visiting it.
- Files outside `public/` are never served. Dotfiles inside it are refused
  with the same 404 as a file that does not exist.
- Path traversal, workspace crossing and hidden-file access each have a
  regression test written from a hole found by auditing a running instance.
- `python -m app.cli preflight` executes the SECURITY.md checklist and refuses
  to pass on a default secret key.

## Errors teach

Every failure carries a `fix` field saying what to do instead — it is a
required argument of the error constructor, so an error without one does not
compile. Every success carries a `guide` saying what usually comes next. An
agent that has never seen this API can finish a task from the error messages.

## Install

**By an agent, end to end** — hand it [`ForLLMInstall.md`][(ForLLMInstall.md](https://github.com/JCOMAIA/AgentSpaceRelease/blob/main/source/ForLLMInstall.md)).
It picks the shape, detects Docker, writes the `.env`, runs the migrations and
prints the API key with an MCP snippet ready to paste.

**By hand**

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # set SECRET_KEY; SANDBOX_DRIVER=none to start
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app
```

**Docker** — `docker compose up`, which runs migrations once in the entrypoint
rather than racing them across workers.

For a public box, [`docs/DEPLOY_VPS.md`](https://github.com/JCOMAIA/AgentSpaceRelease/blob/main/source/docs/DEPLOY_VPS.md) covers a hardened
systemd unit, nightly hard-linked backups and TLS.

## Tested

377 tests, no Docker needed, covering the publish loop, workspace isolation,
path-traversal and hidden-file refusal, the sandbox CSP, cache validators and
conditional requests, quotas and the host disk floor, admission control, MCP
and A2A conformance, subscription entitlement, and the two manuals agreeing
with each other. 12 more assert the container isolation properties against a
real daemon.

This tarball was extracted into an empty directory, installed into a fresh
virtualenv and run before release: 377 passed, the process migrated its own
database and served `/api/v1/hello` and `/pricing`.

## Not in this release

- No self-service password reset. The operator runs
  `python -m app.cli user password <name>`; the server sends no email.
- No terms of service or privacy policy. Add your own before taking money.
- No cap on total accounts and no cleanup of abandoned ones. `INVITE_REQUIRED`
  is the lever if a public instance grows faster than its disk.

## Licence

[Apache 2.0](LICENSE). Use it, fork it, run it as a service, sell it.
"AgentSpace" is a project mark; the licence does not grant it for a derivative
service.
