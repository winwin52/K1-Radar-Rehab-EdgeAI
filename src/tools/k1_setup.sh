#!/usr/bin/env bash
# One-time K1 environment setup.
# Run on K1 (after first deploy.sh push) to install missing system + Python deps.
#
# Usage on K1:
#   cd ~/radar_r/realtime3.0
#   bash tools/k1_setup.sh

set -euo pipefail

echo "==> K1 environment setup"
echo "    User: $(whoami)"
echo "    Host: $(hostname)"
echo "    Pwd:  $(pwd)"
echo

# ─── System packages (need sudo) ───
echo "==> Checking system dependencies (may prompt for sudo password)"

NEED_APT=()

# Sans CJK font for crisp UI rendering (Serif CJK is pre-installed but blocky)
if ! fc-list 2>/dev/null | grep -qi "noto sans cjk sc:"; then
  NEED_APT+=(fonts-noto-cjk)
fi

# SDL2 + image libs for pygame (usually pre-installed on Bianbu but verify)
for lib in libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-ttf-2.0-0; do
  if ! dpkg -l "$lib" 2>/dev/null | grep -q '^ii'; then
    NEED_APT+=("$lib")
  fi
done

# PortAudio runtime for sounddevice (Phase 7)
if ! dpkg -l libportaudio2 2>/dev/null | grep -q '^ii'; then
  NEED_APT+=(libportaudio2)
fi
# libsndfile1 for soundfile.read (Phase 7)
if ! dpkg -l libsndfile1 2>/dev/null | grep -q '^ii'; then
  NEED_APT+=(libsndfile1)
fi

if [[ ${#NEED_APT[@]} -gt 0 ]]; then
  echo "    Installing: ${NEED_APT[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${NEED_APT[@]}"
else
  echo "    All system deps OK."
fi
echo

# ─── Python venv ───
VENV="${VENV:-$HOME/radar}"
if [[ ! -f "$VENV/bin/activate" ]]; then
  echo "==> Creating venv at $VENV"
  python3 -m venv "$VENV"
fi

# shellcheck disable=SC1090,SC1091
source "$VENV/bin/activate"
echo "==> venv: $VIRTUAL_ENV"
echo "    Python: $(python3 --version)"
echo

# ─── Python packages ───
echo "==> Installing/upgrading Python packages"
pip install --upgrade \
  'pydantic>=2.6' \
  'fastapi>=0.110' \
  'uvicorn[standard]>=0.27' \
  'pyzmq>=25.0' \
  'pygame>=2.5' \
  'qrcode[pil]>=7.4' \
  'httpx>=0.27' \
  'Jinja2>=3.1' \
  'sounddevice>=0.4' \
  'soundfile>=0.12'
echo

# ─── Verify all imports ───
echo "==> Verifying imports"
python3 - << 'PY'
import importlib
mods = ['fastapi', 'uvicorn', 'pydantic', 'zmq', 'pygame', 'qrcode', 'httpx',
        'numpy', 'scipy', 'sklearn', 'spidev', 'lgpio']
fails = []
for m in mods:
    try:
        mod = importlib.import_module(m)
        v = getattr(mod, '__version__', '?')
        print(f'  [OK]   {m:<15} {v}')
    except Exception as e:
        print(f'  [FAIL] {m}: {e}')
        fails.append(m)
import sys; sys.exit(1 if fails else 0)
PY
echo

# ─── Verify project imports ───
echo "==> Verifying project modules import"
python3 - << 'PY'
import sys, importlib
sys.path.insert(0, '.')
mods = ['backend.device_state', 'backend.server', 'backend.plan',
        'backend.patient_store', 'backend.session_fsm',
        'backend.session_manager']
fails = []
for m in mods:
    try:
        importlib.import_module(m)
        print(f'  [OK] {m}')
    except Exception as e:
        print(f'  [FAIL] {m}: {e}')
        fails.append(m)
sys.exit(1 if fails else 0)
PY

# ─── Hardware presence checks ───
echo
echo "==> Hardware probe"
[[ -e /dev/spidev3.0 ]]  && echo "  [OK] /dev/spidev3.0 present (radar bus)"   || echo "  [MISS] /dev/spidev3.0 not found"
[[ -e /dev/gpiochip0 ]]  && echo "  [OK] /dev/gpiochip0 present (GPIO)"        || echo "  [MISS] /dev/gpiochip0 not found"
aplay -l 2>/dev/null | grep -q card && echo "  [OK] audio playback device present" || echo "  [MISS] no aplay device"

echo
echo "✓ K1 setup complete."
echo
echo "To launch:"
echo "  source $VENV/bin/activate"
echo "  python3 -m backend.server               # backend"
echo "  DISPLAY=:0 SDL_VIDEODRIVER=x11 \\"
echo "    python3 -m screen.app --fullscreen     # screen"
