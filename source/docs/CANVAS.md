# The Canvas — design

## What it is

A place where an agent makes something and it is immediately live at a URL.

ChatGPT Sites does this inside OpenAI's product, for OpenAI's model, on OpenAI's
infrastructure, for Business and Enterprise customers. The canvas is the same loop, open
source, hosted anywhere, driven by any agent that speaks HTTP, MCP or A2A.

The wager is not that it is better. It is that the shape of it — **your stuff, any agent,
your machine or ours, exportable whole** — is a shape a model vendor cannot ship without
contradicting itself.

## The loop

```
agent writes a file  →  it is live at a URL  →  the human opens the link
```

Everything else in this document exists to keep that loop to three steps.

Today it is four: write, then `POST /deployments`, then live. The deploy call is
bureaucracy — it made sense when the product was a workspace with several projects, and it
makes no sense when the product is a canvas.

**Proposal: `public/` is always served.** Anything an agent writes into
`workspace/public/` is live at `https://<host>/u/<name>/` with no second call. Named
deployments stay for people who want several things at once, or a service with a port.

That removes a concept from the agent's model of the place, which is the only kind of
simplification that counts.

## The insight that shapes everything: publishing is not executing

These are separate capabilities and they have wildly different costs.

| Capability | Needs | Cost to host |
|---|---|---|
| **Publish** — files served at a URL | disk | pennies; a static file is the cheapest thing on the internet |
| **Execute** — run code against those files | a container runtime | memory, CPU, isolation, an operator |
| **Host** — long-running services with a port | a container runtime, always on | the expensive one |

A client-side app — a game, a dashboard, a data visualisation, a landing page, a React
toy — is **entirely publish**. The code runs in the visitor's browser. The agent needs
somewhere to put files and a URL, and nothing else.

This means:

- **The free demo can be publish-only**, and still do most of what ChatGPT Sites does.
- **Self-hosters get execute and host for free**, because they already have Docker.
- The expensive tier is optional rather than foundational.

### Three tiers, one codebase

| Tier | `SANDBOX_DRIVER` | Runs on |
|---|---|---|
| Publish only | `none` *(to build)* | any PaaS free tier, no Docker |
| Publish + execute + host | `remote` (broker) | a VPS, a laptop, the Kimsufi |
| Development | `local_unsafe` | a laptop, no isolation, never exposed |

The driver interface already supports this. A `NullDriver` that refuses execution is a
small addition, and — this is the point — **its refusal should teach**:

> `execution_unavailable`: This instance publishes files but does not run code.
> **Fix:** Write the app as HTML, CSS and JavaScript so it runs in the visitor's
> browser, and put it in `public/`. That covers dashboards, games, visualisations
> and anything with a UI. If you need server-side execution, this instance cannot
> provide it — run your own with Docker, see ForLLMInstall.md.

An agent that reads that does not retry; it changes approach. That is the whole
justification for the teaching layer, applied to a business constraint.

## What changes from what exists

Small list, deliberately.

1. **`public/` is live without a deploy call.** The single biggest simplification.
2. **`/u/<name>/` becomes an index when there is no `public/index.html`** — a generated
   gallery of everything published, with titles read from each page's `<title>`. "Here is
   what I made" for free, and it makes an empty canvas explain itself instead of 404ing.
3. **A `none` sandbox driver** that refuses execution with the guidance above.
4. **A `/connect` page** carrying the MCP snippet, the A2A card URL and the key, with a
   copy button. Onboarding is the product for a first-time user, and right now it is a
   line in a text file.
5. **Egress allowlist for execution tiers** — a proxy permitting PyPI and npm and nothing
   else. Without it an agent cannot install anything, which contradicts the whole "it is a
   computer, not a tool" argument. With unrestricted egress you have donated an open proxy
   to the internet. The allowlist is the only version that is both.

Everything else already exists: the three doors, the teaching layer, quotas, the isolation
work, the operator CLI.

## The demo

Two constraints worth deciding on before choosing where to put it.

**Ephemeral disk erases the point.** Most free tiers give no persistent volume, so a
restart takes the user's work with it. A canvas that loses what you made is not a canvas.
Either pay for a small volume, or label the demo as scratch and mean it — a banner saying
content is deleted regularly, and actually delete it, so nobody learns the lesson the
painful way.

**Public + agents + no moderation is a liability.** Anyone can point an agent at a public
canvas and publish anything, under your domain. Before it is open to the internet, pick
one: invite codes (already built), a hard cap on published bytes per account, and a
documented takedown address. This is the constraint most likely to end a small project,
and it costs nothing to plan for and a lot to retrofit.

Given both, **Fly.io or a small VPS fits this codebase better than Render or Cloudflare**:
persistent volumes are cheap, and Docker works properly if you later want the execute tier.
Cloudflare Workers cannot run this application at all without a rewrite — it is Python and
FastAPI, not a Worker.

## Mobile

The strongest argument for the canvas is the person with no terminal. That makes
onboarding the whole product for them, and it currently reads: find a text file, copy a
key, edit JSON.

The realistic path today is that a connector is configured once on a desktop browser and
then used from the phone. So the `/connect` page should be built for **a laptop screen
being read while a phone is in the other hand**: the MCP URL, the key, one copy button,
and a plain sentence about what to paste it into.

Worth measuring before building more: how long from "I have the link" to "my agent wrote
something". If that is over five minutes, nothing else on this list matters.

## Non-goals

Stated so they do not creep in.

- **Not a social network.** Profiles, follows and feeds are a different product with
  different economics and a moderation burden that ends small teams. The canvas can exist
  without it; it cannot exist without it working first.
- **No authentication for published apps.** Everything in `public/` is public. An app that
  needs its own users needs the execute tier and a real design.
- **No managed database for built apps.** A file in the workspace is a database — SQLite
  is already there, and it exports with everything else.
- **No model provisioning.** The user brings their own agent and their own key. That is
  what makes the economics work and what makes it agnostic; they are the same decision.

## The order

1. `public/` always live, and the generated index. This is the canvas.
2. The `none` driver with a refusal that teaches. This is what makes a free demo honest.
3. `/connect`. This is what makes a stranger succeed.
4. Egress allowlist. This is what makes it a computer.

Steps 1–3 are small and make the demo possible. Step 4 is the one that changes what the
product is, and it should wait until someone has used the first three for something real.
