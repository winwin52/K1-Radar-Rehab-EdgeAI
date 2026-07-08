#!/usr/bin/env bash
# Convenience launcher for K1 — starts backend (+ optional screen).
#
# Usage:
#   bash tools/k1_start.sh                    # backend only (foreground)
#   bash tools/k1_start.sh --with-screen      # backend (background) + screen (foreground)
#   bash tools/k1_start.sh --background       # backend in background, print PID
#
# To stop the backend started in background:
#   pkill -f 'backend.server'
#   # or use the recorded PID:  kill $(cat /tmp/rehab_backend.pid)

set -euo pipefail

WITH_SCREEN=0
BACKGROUND=0
for a in "$@"; do
  case "$a" in
    --with-screen) WITH_SCREEN=1 ;;
    --background)  BACKGROUND=1 ;;
    *) echo "unknown arg: $a"; exit 2 ;;
  esac
done

VENV="${VENV:-$HOME/radar}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ ! -f "$VENV/bin/activate" ]]; then
  echo "✗ venv not found at $VENV. Run tools/k1_setup.sh first."
  exit 1
fi

# shellcheck disable=SC1090,SC1091
source "$VENV/bin/activate"
cd "$PROJECT_ROOT"

# Helpful info banner
IP=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)
[[ -z "$IP" ]] && IP=$(hostname -I | awk '{print $1}')
echo "═══════════════════════════════════════════════════════"
echo " Rehab K1 — starting"
echo "   Project: $PROJECT_ROOT"
echo "   Venv:    $VENV"
echo "   IP:      $IP  →  http://${IP}:8000"
echo "═══════════════════════════════════════════════════════"

start_backend() {
  if [[ $BACKGROUND -eq 1 ]] || [[ $WITH_SCREEN -eq 1 ]]; then
    echo "==> Starting backend (background)"
    nohup python3 -m backend.server > /tmp/rehab_backend.log 2>&1 &
    echo $! > /tmp/rehab_backend.pid
    sleep 2
    if ! kill -0 "$(cat /tmp/rehab_backend.pid)" 2>/dev/null; then
      echo "✗ Backend failed to start. Log:"
      tail -20 /tmp/rehab_backend.log
      exit 1
    fi
    echo "    PID $(cat /tmp/rehab_backend.pid), log /tmp/rehab_backend.log"
  else
    echo "==> Starting backend (foreground; Ctrl-C to stop)"
    exec python3 -m backend.server
  fi
}

start_screen() {
  if [[ -z "${DISPLAY:-}" ]]; then
    export DISPLAY=:0
  fi
  # FORCE x11 — Bianbu's default Wayland session leaks SDL_VIDEODRIVER=wayland
  # into our env, but our pygame 2.6 wheel doesn't have Wayland support.
  # Override with REHAB_SDL_DRIVER if you actually want something else.
  export SDL_VIDEODRIVER="${REHAB_SDL_DRIVER:-x11}"
  echo "==> Starting screen (DISPLAY=$DISPLAY SDL_VIDEODRIVER=$SDL_VIDEODRIVER)"
  exec python3 -m screen.app --fullscreen
}

start_backend
if [[ $WITH_SCREEN -eq 1 ]]; then
  start_screen
fi
