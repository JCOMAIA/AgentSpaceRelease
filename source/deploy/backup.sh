#!/usr/bin/env bash
#
# Nightly backup. Two things with very different shapes:
#
#   the database   small, changes constantly, useless if torn mid-write
#   the workspaces large, mostly static, and the part people would actually miss
#
# Installed as /usr/local/bin/agentspace-backup by deploy/install.sh.

set -euo pipefail

APP_DIR="${APP_DIR:-/srv/agentspace}"
DEST="${BACKUP_DIR:-/var/backups/agentspace}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M)"

mkdir -p "$DEST"

# `.backup` takes a consistent snapshot of a live database. Copying the file
# with cp while the server is writing produces something that restores fine
# right up until the night you need it.
sqlite3 "$APP_DIR/data/agentspace.db" ".backup '$DEST/db-$STAMP.sqlite'"
gzip -f "$DEST/db-$STAMP.sqlite"

# Hard-linked against yesterday: unchanged files cost no extra disk, so keeping
# two weeks of daily snapshots costs about one copy plus what actually changed.
LATEST="$DEST/workspaces-latest"
TODAY="$DEST/workspaces-$STAMP"
if [ -d "$LATEST" ]; then
    rsync -a --delete --link-dest="$LATEST" "$APP_DIR/data/workspaces/" "$TODAY/"
else
    rsync -a "$APP_DIR/data/workspaces/" "$TODAY/"
fi
rm -f "$LATEST"
ln -s "$TODAY" "$LATEST"

find "$DEST" -maxdepth 1 -name 'db-*.sqlite.gz' -mtime "+$KEEP_DAYS" -delete
find "$DEST" -maxdepth 1 -type d -name 'workspaces-*' -mtime "+$KEEP_DAYS" \
    -exec rm -rf {} +

FREE="$(df -h "$APP_DIR" | awk 'NR==2 {print $4}')"
echo "$(date -u +%FT%TZ) backup ok — $FREE free on $(df -h "$APP_DIR" | awk 'NR==2 {print $6}')"

# A backup that lives only on the machine it is backing up is a copy, not a
# backup. Point this at somewhere else once there is anything worth keeping:
#   rclone sync "$DEST" remote:agentspace-backups
