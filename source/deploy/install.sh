#!/usr/bin/env bash
#
# Provision a publish-only AgentSpace on a fresh Ubuntu box.
#
#   sudo bash deploy/install.sh
#
# Idempotent: safe to run again after a `git pull` to pick up new code.
#
# Deliberately does NOT install Docker. This space publishes files and does not
# execute anything, and the cleanest way to guarantee that is for the machine to
# have no container runtime at all. There is no socket to leak, no image to
# escape, and no daemon to keep patched.

set -euo pipefail

APP_USER="${APP_USER:-agentspace}"
APP_DIR="${APP_DIR:-/srv/agentspace}"
REPO="${REPO:-https://github.com/JCOMAIA/AgentSpace.git}"
PORT="${PORT:-8000}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31mStopped:\033[0m %s\n\n  Fix: %s\n\n' "$1" "$2" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "this script needs root" "run it with sudo"

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 curl ca-certificates
info "python $(python3 --version | cut -d' ' -f2)"

say "Service account"
if id "$APP_USER" >/dev/null 2>&1; then
    info "user $APP_USER already exists"
else
    # No login shell and no home: this account exists to own files and run one
    # process. If someone ever gets the ability to run commands as it, there is
    # nothing here for them.
    adduser --system --group --no-create-home --shell /usr/sbin/nologin "$APP_USER"
    info "created $APP_USER"
fi

say "Code"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
    info "updated $APP_DIR"
else
    mkdir -p "$(dirname "$APP_DIR")"
    git clone --depth 1 "$REPO" "$APP_DIR"
    info "cloned into $APP_DIR"
fi
mkdir -p "$APP_DIR/data/workspaces"

say "Python environment"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"
info "dependencies installed"

say "Configuration"
if [ -f "$APP_DIR/.env" ]; then
    info ".env already exists — leaving it alone"
else
    read -rp "  Public URL (e.g. https://made.gg): " PUBLIC_URL
    [ -n "$PUBLIC_URL" ] || die "a public URL is required" \
        "every link handed to an agent is built from it; localhost sends visitors to their own machine"
    BASE_DOMAIN="${PUBLIC_URL#https://}"
    BASE_DOMAIN="${BASE_DOMAIN#http://}"

    cat > "$APP_DIR/.env" <<EOF
# Written by deploy/install.sh — publish-only VPS.
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
PUBLIC_URL=${PUBLIC_URL}
BASE_DOMAIN=${BASE_DOMAIN}

DATABASE_URL=sqlite+aiosqlite:///./data/agentspace.db
DATA_ROOT=./data/workspaces

# No sandbox, no broker, no Docker socket. See app/sandbox/none_driver.py.
SANDBOX_DRIVER=none

# Migrations run from the systemd unit, once, before the server starts.
AUTO_MIGRATE=false

# Invite-only to begin with. Opening the door is one edit and a restart; getting
# a spam wave off a public box is not.
REGISTRATION_OPEN=true
INVITE_REQUIRED=true

# Writes stop while free disk is under this, above every plan. A full disk does
# not just reject writes, it corrupts the database.
DISK_RESERVE_MB=5120
EOF
    info "wrote $APP_DIR/.env with a fresh secret key"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

say "Database"
sudo -u "$APP_USER" env -C "$APP_DIR" "$APP_DIR/.venv/bin/alembic" upgrade head
info "schema at head"

say "Service"
install -m 644 "$APP_DIR/deploy/agentspace.service" /etc/systemd/system/agentspace.service
systemctl daemon-reload
systemctl enable --now agentspace
sleep 2

if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null; then
    info "agentspace is answering on 127.0.0.1:${PORT}"
else
    die "the service did not come up" "journalctl -u agentspace -n 50 --no-pager"
fi

say "Backups"
install -m 755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/agentspace-backup
cat > /etc/cron.d/agentspace-backup <<'EOF'
# Nightly at 04:12. Not on the hour: everything else on the internet runs then.
12 4 * * * root /usr/local/bin/agentspace-backup >> /var/log/agentspace-backup.log 2>&1
EOF
info "nightly backup installed"

say "Done"
cat <<EOF

  The space is running on 127.0.0.1:${PORT} and is not reachable from outside yet.
  Nothing is listening on a public port, which is deliberate.

  Next, put Cloudflare in front — see docs/DEPLOY_VPS.md. Then:

      sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python -m app.cli preflight
      sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python -m app.cli invite --count 10

EOF
