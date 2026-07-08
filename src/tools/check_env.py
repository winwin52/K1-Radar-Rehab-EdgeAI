#!/usr/bin/env python3
"""
Cross-platform environment health check.

Run from project root:
    python tools/check_env.py           # local (dev or K1)
    python tools/check_env.py --remote winwin51@10.126.135.110

Verifies:
  - Python version
  - Required packages importable
  - Project modules importable
  - Hardware presence (K1 only: /dev/spidev*, /dev/gpiochip*, audio)
  - Font availability (CJK fonts for screen)
  - Config files present
  - Disk space
"""

from __future__ import annotations

import argparse
import importlib
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# Force UTF-8 stdout on Windows so checkmarks/Chinese render
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

# Disable color on Windows cmd.exe (works in Git Bash though)
if platform.system() == "Windows" and not os.environ.get("MSYSTEM"):
    GREEN = RED = YELLOW = RESET = ""


def ok(msg):    print(f"  {GREEN}[OK]{RESET}   {msg}")
def fail(msg):  print(f"  {RED}[FAIL]{RESET} {msg}")
def warn(msg):  print(f"  {YELLOW}[WARN]{RESET} {msg}")
def info(msg):  print(f"  · {msg}")


def section(title):
    print()
    print(f"━━━ {title} ━━━")


def check_python():
    section("Python")
    ver = sys.version_info
    if (ver.major, ver.minor) >= (3, 11):
        ok(f"Python {ver.major}.{ver.minor}.{ver.micro}")
    else:
        fail(f"Python {ver.major}.{ver.minor}.{ver.micro} — need 3.11+")
    info(f"Platform: {platform.system()} {platform.machine()} ({platform.release()})")
    info(f"venv:     {sys.prefix}")


def check_packages():
    section("Required packages")
    required = [
        ("fastapi",   "Backend HTTP framework"),
        ("uvicorn",   "ASGI server"),
        ("pydantic",  "Data validation (>=2)"),
        ("zmq",       "Inter-process messaging"),
        ("pygame",    "Screen renderer"),
        ("qrcode",    "QR code generation"),
        ("httpx",     "HTTP client (for LLM API)"),
    ]
    optional = [
        ("numpy",        "Sensing — Phase 5"),
        ("scipy",        "Sensing — Phase 5"),
        ("sklearn",      "Emotion classifier — Phase 5"),
        ("spidev",       "Radar SPI — Phase 5 (K1 only)"),
        ("lgpio",        "Radar GPIO — Phase 5 (K1 only)"),
        ("sounddevice",  "Audio — Phase 4 (optional)"),
    ]

    fails = 0
    for name, desc in required:
        try:
            mod = importlib.import_module(name)
            v = getattr(mod, "__version__", "?")
            ok(f"{name:<14} {v:<12} ({desc})")
        except ImportError:
            fail(f"{name:<14} NOT INSTALLED ({desc})")
            fails += 1

    for name, desc in optional:
        try:
            mod = importlib.import_module(name)
            v = getattr(mod, "__version__", "?")
            ok(f"{name:<14} {v:<12} ({desc})")
        except ImportError:
            warn(f"{name:<14} not installed  ({desc})")
    return fails


def check_project_modules():
    section("Project modules")
    # Add project root to sys.path
    here = Path(__file__).resolve().parent.parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    mods = [
        "backend.device_state",
        "backend.zmq_bridge",
        "backend.plan",
        "backend.patient_store",
        "backend.session_fsm",
        "backend.session_manager",
        "backend.server",
    ]
    fails = 0
    for m in mods:
        try:
            importlib.import_module(m)
            ok(m)
        except Exception as e:
            fail(f"{m}: {type(e).__name__}: {e}")
            fails += 1
    return fails


def check_config():
    section("Config files")
    root = Path(__file__).resolve().parent.parent
    expected = [
        ("config/system.toml",         True),
        ("config/llm.toml",            True),
        ("config/default_plan.json",   True),
        ("config/secrets.env",         False),  # required only when LLM in use
        ("prompts/assessment_v1.md",   True),
        ("prompts/encourage_v1.md",    True),
    ]
    for rel, required in expected:
        p = root / rel
        if p.exists():
            ok(f"{rel}")
        elif required:
            fail(f"{rel} missing")
        else:
            warn(f"{rel} not yet configured")


def check_hardware():
    section("Hardware (K1-specific)")
    if platform.system() != "Linux":
        info("Skipping (not Linux)")
        return
    # SPI
    spi = Path("/dev/spidev3.0")
    if spi.exists(): ok(f"{spi} (radar SPI bus)")
    else:            warn(f"{spi} not present — radar will not work (Phase 5+)")
    # GPIO
    gpio = Path("/dev/gpiochip0")
    if gpio.exists(): ok(f"{gpio} (GPIO)")
    else:             warn(f"{gpio} not present")
    # Audio
    try:
        out = subprocess.check_output(["aplay", "-l"], stderr=subprocess.DEVNULL,
                                       timeout=2, text=True)
        if "card" in out.lower():
            cards = [l for l in out.splitlines() if l.startswith("card")]
            ok(f"audio playback device(s): {len(cards)}")
            for c in cards[:3]:
                info(f"  {c}")
        else:
            warn("aplay reports no cards")
    except FileNotFoundError:
        warn("aplay not installed (audio playback may not work)")
    except Exception as e:
        warn(f"audio check failed: {e}")


def check_fonts():
    section("Fonts (Chinese CJK for screen)")
    if platform.system() == "Linux":
        try:
            out = subprocess.check_output(["fc-list"], text=True, timeout=2)
            sans = [l for l in out.splitlines() if "Noto Sans CJK" in l]
            serif = [l for l in out.splitlines() if "Noto Serif CJK" in l]
            wqy = [l for l in out.splitlines() if "WenQuanYi" in l or "微米" in l.lower()]
            if sans:   ok(f"Noto Sans CJK ({len(sans)} variants)")
            elif serif: warn("Only Serif CJK present — install fonts-noto-cjk for crisper UI")
            elif wqy:  ok(f"WenQuanYi present ({len(wqy)})")
            else:      fail("No CJK font found — install fonts-noto-cjk")
        except Exception as e:
            warn(f"fc-list check failed: {e}")
    else:
        info("Skipped on non-Linux (system fonts assumed)")


def check_network():
    section("Network")
    hostname = socket.gethostname()
    info(f"hostname: {hostname}")
    # local IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        ok(f"local IP: {ip}")
    except Exception as e:
        warn(f"could not detect local IP: {e}")
    finally:
        s.close()


def check_disk():
    section("Disk")
    here = Path(__file__).resolve().parent.parent
    total, used, free = shutil.disk_usage(here)
    gb = lambda b: b / (1024**3)
    if gb(free) > 1.0:
        ok(f"free space: {gb(free):.1f} GB (of {gb(total):.1f} GB)")
    else:
        fail(f"only {gb(free):.2f} GB free — may not be enough for sessions")


def run_local():
    print(f"\n┌─ Environment check: {platform.node()} ─┐")
    check_python()
    fails = check_packages()
    fails += check_project_modules()
    check_config()
    check_hardware()
    check_fonts()
    check_network()
    check_disk()
    print()
    if fails == 0:
        print(f"{GREEN}✓ All required checks passed.{RESET}")
        return 0
    else:
        print(f"{RED}✗ {fails} required check(s) failed.{RESET}")
        return 1


def run_remote(target):
    """SSH to target and run this script there."""
    here = Path(__file__).resolve()
    print(f"==> Running check on remote: {target}")
    # Push the script and run inline; doesn't require code be deployed yet
    with open(here, "rb") as f:
        script = f.read()
    proc = subprocess.run(
        ["ssh", target, "python3 - --local"],
        input=script,
        check=False,
    )
    return proc.returncode


def main():
    ap = argparse.ArgumentParser(description="Environment health check.")
    ap.add_argument("--remote", metavar="user@host",
                    help="Run check on remote machine via SSH")
    ap.add_argument("--local", action="store_true",
                    help="(internal) skip remote dispatch and run here")
    args = ap.parse_args()

    if args.remote and not args.local:
        sys.exit(run_remote(args.remote))
    else:
        sys.exit(run_local())


if __name__ == "__main__":
    main()
