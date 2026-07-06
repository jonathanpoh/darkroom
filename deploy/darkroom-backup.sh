#!/usr/bin/env bash
# Nightly catalog backup (W9): VACUUM INTO a dated local copy, prune old
# copies, then push to the NAS over scp/SFTP.
#
# VACUUM INTO produces a consistent point-in-time snapshot even while the
# darkroom-api server is writing (safe under WAL), which is why we never
# copy the live DB file directly. Runs as the same user as darkroom-api
# via darkroom-backup.service/.timer (systemctl link both, enable the timer).
#
# scp (SFTP), not rsync: Synology's patched rsync refuses --server mode
# unless DSM's rsync service is running ("rsync service is no running",
# code 43), a whole extra service to keep enabled for a single small file.
# The SFTP subsystem is already on for the backup user, so scp + a remote
# find-prune does the same mirror job with no DSM dependency.
#
# Config (via /etc/darkroom/env or the unit's Environment= lines):
#   DARKROOM_CATALOG       live DB path   (default /var/lib/darkroom/astro_catalog.db)
#   DARKROOM_BACKUP_DIR    local backups  (default /var/lib/darkroom/backups)
#   DARKROOM_BACKUP_KEEP   days retained, locally and on the NAS (default 14)
#   DARKROOM_BACKUP_DEST   scp target, e.g. user@nas:/volume1/backups/darkroom
#                          unset -> local backup only, push skipped
#   DARKROOM_BACKUP_SSH_PORT  ssh port on the NAS (default 22)
#   DARKROOM_BACKUP_SSH_KEY  identity for scp/ssh
#                          (default ~/.ssh/id_ed25519_nas_backup)
set -euo pipefail

DB="${DARKROOM_CATALOG:-/var/lib/darkroom/astro_catalog.db}"
BACKUP_DIR="${DARKROOM_BACKUP_DIR:-/var/lib/darkroom/backups}"
KEEP_DAYS="${DARKROOM_BACKUP_KEEP:-14}"
DEST="${DARKROOM_BACKUP_DEST:-}"
SSH_PORT="${DARKROOM_BACKUP_SSH_PORT:-22}"
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
    REMOTE_USER_HOST="${DEST%%:*}"
    REMOTE_PATH="${DEST#*:}"
    scp -P "$SSH_PORT" -i "$SSH_KEY" -o BatchMode=yes "$SNAP" "$DEST/"
    ssh -p "$SSH_PORT" -i "$SSH_KEY" -o BatchMode=yes "$REMOTE_USER_HOST" \
        "find '$REMOTE_PATH' -name 'astro_catalog-*.db' -mtime +$KEEP_DAYS -delete"
    echo "pushed $SNAP -> $DEST/ (pruned >$KEEP_DAYS days remotely)"
else
    echo "DARKROOM_BACKUP_DEST unset; local backup only, push skipped"
fi
