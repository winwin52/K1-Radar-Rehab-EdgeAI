"""
Session lifecycle endpoints — /api/session/*

Each control endpoint:
  - logs the call (so K1 backend.log shows the full command trail);
  - returns the *current* device status synchronously, so the client gets an
    immediate confirmation even if the WebSocket push is briefly delayed
    (matters a lot on flaky mobile networks where the WS frame may arrive
    seconds after the HTTP 200);
  - is idempotent where it makes sense (abort = no-op if already not running).

Phase 8 (control-link hardening): the previous version returned bare {ok:true}
which meant the mobile client had no way to know whether pause/stop actually
hit the FSM. With status echo, the client can update the UI from the HTTP
response without waiting for the WS broadcast.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..device_state import get_manager as get_device_manager
from ..session_manager import SessionManagerError, get_session_manager

log = logging.getLogger(__name__)
router = APIRouter()


# ---- Models ---------------------------------------------------------

class SessionStartRequest(BaseModel):
    patient_name: str = Field(..., min_length=1, max_length=64)
    plan_override: Optional[Dict[str, Any]] = None


# ---- Helpers --------------------------------------------------------

def _status_payload() -> Dict[str, Any]:
    """Snapshot of the device status — returned by every control endpoint
    so the web/mobile client can update its UI atomically."""
    return get_device_manager().get_status()


def _fsm_diag() -> Dict[str, Any]:
    """Internal FSM flags — used by /diag for live debugging.
    Safe to call when no session is running (returns running=False)."""
    sm = get_session_manager()
    fsm = sm.current_fsm
    if fsm is None:
        return {"running": False}
    return {
        "running": True,
        "paused":   fsm.is_paused(),
        "aborting": fsm.is_aborting(),
        "patient":  fsm.patient,
        "session_id": fsm.session_id,
    }


# ---- Lifecycle endpoints --------------------------------------------

@router.post("/start")
async def session_start(req: SessionStartRequest):
    sm = get_session_manager()
    log.info("[session.start] patient=%r override=%r", req.patient_name, req.plan_override)
    try:
        info = await sm.start(req.patient_name, req.plan_override)
    except SessionManagerError as e:
        log.warning("[session.start] rejected: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "ok": True,
        "session_id": info["session_id"],
        "plan": info["plan"],
        "estimated_duration_s": info["estimated_duration_s"],
        "status": _status_payload(),
    }


@router.post("/stop")
async def session_stop():
    """Graceful stop — FSM marks abort, finalize writes whatever is done.

    Idempotent: if no session is running, returns ok=true with a hint."""
    sm = get_session_manager()
    log.info("[session.stop] running=%s", sm.is_running)
    if not sm.is_running:
        return {"ok": True, "already_idle": True, "status": _status_payload()}
    try:
        await sm.stop()
    except SessionManagerError as e:
        log.warning("[session.stop] rejected: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "msg": "已发出结束信号", "status": _status_payload()}


@router.post("/abort")
async def session_abort():
    """Force abort — same effect as stop in current design, but the route is
    kept distinct so we can add discard-data semantics later. Always
    idempotent (never 4xx for the client; UI can show generic toast)."""
    sm = get_session_manager()
    log.info("[session.abort] running=%s", sm.is_running)
    try:
        await sm.abort()
    except SessionManagerError as e:
        log.info("[session.abort] no-op: %s", e)
    return {"ok": True, "status": _status_payload()}


@router.post("/pause")
async def session_pause():
    sm = get_session_manager()
    log.info("[session.pause] running=%s", sm.is_running)
    try:
        sm.pause()
    except SessionManagerError as e:
        log.warning("[session.pause] rejected: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "paused": True, "status": _status_payload()}


@router.post("/resume")
async def session_resume():
    sm = get_session_manager()
    log.info("[session.resume] running=%s", sm.is_running)
    try:
        sm.resume()
    except SessionManagerError as e:
        log.warning("[session.resume] rejected: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "paused": False, "status": _status_payload()}


@router.post("/skip_set")
async def session_skip_set():
    sm = get_session_manager()
    log.info("[session.skip_set] running=%s", sm.is_running)
    try:
        sm.skip_set()
    except SessionManagerError as e:
        log.warning("[session.skip_set] rejected: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "msg": "跳过本组", "status": _status_payload()}


# ---- Empathy intercept (Phase 9) ------------------------------------

class EmpathyChoiceRequest(BaseModel):
    """One of the option strings broadcast in device_state.empathy_request."""
    choice: str = Field(..., min_length=1, max_length=32)


@router.post("/empathy_choice")
async def session_empathy_choice(req: EmpathyChoiceRequest):
    """
    Patient chose a softening option from the empathy modal on their phone.

    Choices currently understood:
      "continue"  — do nothing, just close the intercept
      "reduce_2"  — skip 2 reps from the current set (best-effort: implemented
                    as skip_set fallback if individual rep-skip isn't wired)
      "rest_1m"   — pause for ~60s, then auto-resume
      "skip_set"  — skip the rest of the current set

    Always idempotent and forgiving — an unknown choice or one arriving after
    the intercept already closed will just clear the request and return ok=True.
    The point is to make the patient feel agency, not to enforce a state machine.
    """
    sm = get_session_manager()
    mgr = get_device_manager()
    choice = req.choice
    log.info("[session.empathy_choice] %r (running=%s)", choice, sm.is_running)

    if not sm.is_running:
        # Stale tap from a session that already ended — just clear.
        await mgr.set_empathy_request(None)
        return {"ok": True, "applied": "noop_idle", "status": _status_payload()}

    applied = "noop"
    try:
        if choice == "skip_set" or choice == "reduce_2":
            sm.skip_set()
            applied = "skipped_set"
        elif choice == "rest_1m":
            sm.pause()
            applied = "paused"
            # Schedule auto-resume after 60s
            import asyncio
            async def _auto_resume():
                try:
                    await asyncio.sleep(60.0)
                    if sm.is_running and sm.current_fsm and sm.current_fsm.is_paused():
                        sm.resume()
                        log.info("[empathy] auto-resumed after 60s rest")
                except Exception as e:
                    log.warning("[empathy] auto-resume failed: %r", e)
            asyncio.create_task(_auto_resume(), name="empathy-auto-resume")
        elif choice == "continue":
            applied = "continued"
        else:
            log.warning("[empathy] unknown choice: %r", choice)
            applied = "unknown_choice"
    except SessionManagerError as e:
        log.warning("[empathy] action failed: %r", e)
        applied = f"failed:{e}"

    # Always clear the request once we've handled it
    await mgr.set_empathy_request(None)
    return {"ok": True, "applied": applied, "status": _status_payload()}


# ---- Read-only endpoints --------------------------------------------

@router.get("/current")
async def session_current():
    """Snapshot of current device + session state."""
    mgr = get_device_manager()
    return {
        "active": mgr.is_working(),
        "status": mgr.get_status(),
    }


@router.get("/diag")
async def session_diag():
    """Internal diagnostic — returns FSM flags + device status together.

    Used by the mobile UI's "调试" 抽屉, and by k1 debug sessions when a
    button "feels dead" — you can curl this and confirm whether the FSM
    saw the abort flag."""
    return {
        "fsm":    _fsm_diag(),
        "device": _status_payload(),
    }
