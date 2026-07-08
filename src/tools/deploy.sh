#!/usr/bin/env bash
# Deploy realtime3.0 from Windows dev machine to K1 device.
#
# Usage:
#   ./tools/deploy.sh              # push code (excluding runtime data)
#   ./tools/deploy.sh --pull-data  # also pull patient data back to dev for backup
#
# Configuration: edit K1_USER and K1_HOST below, or override via env vars:
#   K1_USER=winwin51 K1_HOST=10.126.135.110 ./tools/deploy.sh

set -euo pipefail

K1_USER="${K1_USER:-winwin51}"
K1_HOST="${K1_HOST:-10.126.135.110}"
K1_DEST="${K1_DEST:-/home/${K1_USER}/radar_r/realtime3.0}"

# Resolve project root (parent of this script's directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "==> Source:      $PROJECT_ROOT"
echo "==> Destination: ${K1_USER}@${K1_HOST}:${K1_DEST}"

# Quick SSH connectivity check
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${K1_USER}@${K1_HOST}" true 2>/dev/null; then
  echo "✗ Cannot SSH to ${K1_USER}@${K1_HOST}. Check:"
  echo "  - K1 powered on and on same network"
  echo "  - SSH key in ~/.ssh/authorized_keys on K1 (or be ready to enter password)"
  echo "  - Try: ssh ${K1_USER}@${K1_HOST}"
  exit 1
fi

# tar over ssh; tar overlay preserves K1-side patient data
echo "==> Pushing code via tar over ssh..."
tar c \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='*.swp' \
  . | ssh "${K1_USER}@${K1_HOST}" "mkdir -p '${K1_DEST}' && tar x -C '${K1_DEST}'"

echo "==> Done. Files on K1:"
ssh "${K1_USER}@${K1_HOST}" "cd '${K1_DEST}' && find . -type f -not -path './__pycache__/*' -not -name '*.pyc' | wc -l" | xargs -I{} echo "    {} files"

# Optional: pull patients/ back to dev for offline inspection
if [[ "${1:-}" == "--pull-data" ]]; then
  echo "==> Pulling patient data back..."
  mkdir -p "${PROJECT_ROOT}/patients_backup"
  ssh "${K1_USER}@${K1_HOST}" "cd '${K1_DEST}' && tar c patients/ 2>/dev/null || tar c --files-from /dev/null" \
    | tar x -C "${PROJECT_ROOT}/patients_backup"
  echo "    Backup saved to patients_backup/"
fi

cat <<EOM

✓ Deploy complete.

Next steps on K1:
  ssh ${K1_USER}@${K1_HOST}
  cd ${K1_DEST}
  source ~/radar/bin/activate         # activate the existing venv
  python3 -m backend.server           # start backend
  # (in another terminal/tmux pane)
  DISPLAY=:0 SDL_VIDEODRIVER=x11 \\
    python3 -m screen.app --fullscreen  # start screen

Or run remote command:
  ssh ${K1_USER}@${K1_HOST} 'cd ${K1_DEST} && source ~/radar/bin/activate && python3 -m backend.server'
EOM
