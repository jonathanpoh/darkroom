#!/usr/bin/env bash
# Nightly catalog backup (W9): VACUUM INTO a dated local copy, prune old
# copies, then rsync the backup dir to the NAS.
#
# VACUUM INTO produces a consistent point-in-time snapshot even while the
# darkroom-api server is writing (safe under WAL), which is why we never
# copy the live DB file directly. Runs as the same user as darkroom-api
# via darkroom-backup.service/.timer (systemctl link both, enable the timer).
#
# Config (via /etc/darkroom/env, same EnvironmentFile as the API service):
#   DARKROOM_CATALOG       live DB path   (default /var/lib/darkroom/astro_catalog.db)
#   DARKROOM_BACKUP_DIR    local backups  (default /var/lib/darkroom/backups)
#   DARKROOM_BACKUP_KEEP   days retained  (default 14)
#   DARKROOM_BACKUP_DEST   rsync target, e.g. user@nas:/volume1/backups/darkroom/
#                          unset -> local backup only, rsync skipped
#   DARKROOM_BACKUP_SSH_KEY  identity for rsync-over-ssh
#                          (default ~/.ssh/id_ed25519_nas_backup)
set -euo pipefail

DB="${DARKROOM_CATALOG:-/var/lib/darkroom/astro_catalog.db}"
BACKUP_DIR="${DARKROOM_BACKUP_DIR:-/var/lib/darkroom/backups}"
KEEP_DAYS="${DARKROOM_BACKUP_KEEP:-14}"
DEST="${DARKROOM_BACKUP_DEST:-}"
SSH_KEY="${DARKROOM_BACKUP_SSH_KEY:-$HOME/.ssh/id_ed25519_nas_backup}"
PYTHON="/opt/darkroom/.venv/bin/python"

mkdir -p "$BACKUP_DIR"
SNAP="$BACKUP_DIR/astro_catalog-$(date +%F).db"

# Re-running on the same day replaces that day's snapshot.
rm -f "$SNAP"
"$PYTHON" - "$DB" "$SNAP" <<'EOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(src)
conn.execute("VACUUM INTO ?", (dst,))
conn.close()
EOF
echo "snapshot: $SNAP ($(stat -c %s "$SNAP") bytes)"

find "$BACKUP_DIR" -name 'astro_catalog-*.db' -mtime "+$KEEP_DAYS" -delete

if [ -n "$DEST" ]; then
    rsync -a --delete -e "ssh -i $SSH_KEY -o BatchMode=yes" "$BACKUP_DIR/" "$DEST"
    echo "rsynced $BACKUP_DIR/ -> $DEST"
else
    echo "DARKROOM_BACKUP_DEST unset; local backup only, rsync skipped"
fi
