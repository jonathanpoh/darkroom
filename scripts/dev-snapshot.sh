#!/usr/bin/env bash
# Pull the latest nightly catalog backup from the NAS and run the webapi
# against it locally — a disposable staging copy for smoke-testing UI or
# migration changes with real data. Never touches the live (LXC) catalog;
# state changes land in the local snapshot file and evaporate with it.
#
# Usage: scripts/dev-snapshot.sh [port]   (default 8123, token "dev")
#
# Requires the Mac-side NAS key (see BACKLOG W9 backup notes):
#   ~/.ssh/id_ed25519_darkroom_backup, NAS ssh on port 3673.
# scp -O because DSM chroots the SFTP subsystem to /volume1 (see
# deploy/darkroom-backup.sh for the long version).
set -euo pipefail

NAS="${DARKROOM_NAS:-darkroom-backup@192.168.2.17}"
SSH_PORT="${DARKROOM_NAS_SSH_PORT:-3673}"
KEY="${DARKROOM_NAS_SSH_KEY:-$HOME/.ssh/id_ed25519_darkroom_backup}"
REMOTE_DIR="/volume1/backups/darkroom"
DEST_DIR="${DARKROOM_SNAPSHOT_DIR:-$HOME/tmp/darkroom-snapshots}"
PORT="${1:-8123}"

mkdir -p "$DEST_DIR"
LATEST=$(ssh -p "$SSH_PORT" -i "$KEY" -o BatchMode=yes "$NAS" \
    "ls -1 $REMOTE_DIR/astro_catalog-*.db | sort | tail -1")
scp -O -P "$SSH_PORT" -i "$KEY" -o BatchMode=yes "$NAS:$LATEST" "$DEST_DIR/"
SNAP="$DEST_DIR/$(basename "$LATEST")"
echo "snapshot: $SNAP"
echo "serving on http://127.0.0.1:$PORT — login token: \${DARKROOM_API_TOKEN:-dev}"

cd "$(dirname "$0")/.."
DARKROOM_API_TOKEN="${DARKROOM_API_TOKEN:-dev}" DARKROOM_CATALOG="$SNAP" \
    exec uv run uvicorn --factory darkroom.webapi.app:create_app_from_env --port "$PORT"
