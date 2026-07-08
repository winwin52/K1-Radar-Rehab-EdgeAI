"""
Device-level endpoints — /api/device/*

These describe the K1 hardware/software itself, not any patient or session.
"""

import os
import socket
import time

from fastapi import APIRouter

from ..device_state import get_manager
from ..llm import audit as llm_audit
from ..llm import factory as llm_factory

router = APIRouter()


def _get_local_ip() -> str:
    """Best-effort local IP (works across Win/Linux without external deps)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is actually sent; this just picks the default route iface
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@router.get("/info")
async def device_info():
    return {
        "device_name": "k1-rehab",
        "version": "0.1.0-phase1",
        "ip": _get_local_ip(),
        "hostname": socket.gethostname(),
        "http_port": 8000,
    }


@router.get("/status")
async def device_status():
    return get_manager().get_status()


@router.get("/health")
async def device_health():
    """Subsystem health summary."""
    llm_provider = llm_factory.current_provider("assessment")
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()
                    and os.environ.get("DEEPSEEK_API_KEY") != "sk-replace-me")
    return {
        "overall": "ok",
        "subsystems": {
            "radar":  {"status": "depends_on_session", "msg": "starts when session starts"},
            "audio":  {"status": "not_implemented",     "msg": "Phase 7"},
            "screen": {"status": "unknown",             "msg": "depends on screen process"},
            "llm":    {"status": "ok",
                       "provider": llm_provider,
                       "key_present": has_key,
                       "msg":    f"current provider: {llm_provider}"},
        },
        "timestamp": time.time(),
    }


@router.get("/llm_audit")
async def device_llm_audit():
    """Today's LLM call summary (cost/calls/tokens)."""
    return llm_audit.daily_summary()
