"""
SessionManager — owns the lifecycle of the SessionFSM task.

Bridges between HTTP routes (start/stop/pause/resume/skip/abort) and the FSM,
and between FSM state changes and:
  - DeviceStateManager (for global state broadcast: ZMQ + WebSocket)
  - PatientStore       (for finalizing/persisting session data)
  - Sensing process    (Phase 5: real radar or mock; pushes emotion events)
  - Coordinator        (Phase 5: consumes sensing events, drives Coach)

Singleton (one device = at most one running session).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import multiprocessing as mp
import time
from pathlib import Path
from typing import Any

from . import sensing_proc
from .coordinator import get_coordinator
from .device_state import DeviceStateManager, get_manager as get_device_manager
from .patient_store import PatientStore, PatientNotFoundError
from .plan import Plan
from .session_fsm import SessionFSM, SessionResult

log = logging.getLogger(__name__)

# Project root (parent of backend/). Used to feed sensing process the location
# of rehab_engine.py + model.pkl.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

class SessionManagerError(Exception):
    """Raised when an invalid lifecycle operation is requested."""


class SessionManager:
    """Owns the active SessionFSM (or None) and serializes lifecycle ops."""

    def __init__(self, device_state: DeviceStateManager, patient_store: PatientStore,
                 sensing_queue: mp.Queue | None = None):
        self._device = device_state
        self._patients = patient_store
        # Shared queue used by sensing child process → Coordinator. Owned by
        # the backend lifespan (so coordinator and SessionManager share it).
        self._sensing_q: mp.Queue | None = sensing_queue

        self._fsm: SessionFSM | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # Snapshot of inputs (kept for session.json finalization)
        self._current_plan: Plan | None = None
        self._current_patient: str | None = None
        self._current_session_id: str | None = None
        self._session_started_at: float = 0.0

        # Sensing process handle + its stop signal (one set per session)
        self._sensing_proc: mp.Process | None = None
        self._sensing_stop: Any = None    # mp.synchronize.Event

    # ---- Properties ------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_fsm(self) -> SessionFSM | None:
        return self._fsm

    # ---- Lifecycle -------------------------------------------------

    async def start(self, patient_name: str, plan_override: dict | None = None) -> dict:
        """
        Start a new session.

        Raises:
            SessionManagerError — already running / invalid plan / unknown patient
        """
        async with self._lock:
            if self.is_running:
                raise SessionManagerError("已有正在进行的 session,请先结束")

            # Resolve plan: patient default → optional override
            try:
                profile = self._patients.get(patient_name)
                base_plan = Plan.from_dict(profile.default_plan)
            except PatientNotFoundError:
                # Allow on-the-fly anonymous patients (just use clinical default)
                # — they'll get a profile created lazily so history persists.
                base_plan = Plan.from_default()

            plan = base_plan.with_overrides(plan_override)
            errs = plan.validate()
            if errs:
                raise SessionManagerError("计划参数错误: " + "; ".join(errs))

            # Ensure patient directory exists (lazy-create for anonymous flows)
            if not self._patients.exists(patient_name):
                from .patient_store import PatientProfile
                self._patients.create(PatientProfile(
                    name=patient_name,
                    default_plan=plan.to_dict(),
                ))

            session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._current_plan = plan
            self._current_patient = patient_name
            self._current_session_id = session_id
            self._session_started_at = time.time()

            # Wire FSM progress → DeviceStateManager
            async def on_progress(fsm_state: str, progress: dict):
                await self._device.update_progress(fsm_state, progress)

            self._fsm = SessionFSM(
                plan=plan,
                patient=patient_name,
                session_id=session_id,
                on_progress=on_progress,
            )

            # Transition device into WORKING before starting task
            await self._device.to_working(session_id=session_id, patient=patient_name)

            # Phase 9: attach the patient's current Journey snapshot so the
            # screen can render the mountain at the right starting position.
            # We resolve `cycle_weeks` from the profile that exists at this
            # point (either the real one or the just-lazy-created anonymous
            # one with defaults).
            try:
                from .journey import summarize as journey_summarize
                profile = self._patients.get(patient_name)
                gami = journey_summarize(profile.journey,
                                         profile.rehab_cycle_total_weeks)
                gami["cycle_weeks"] = profile.rehab_cycle_total_weeks
                gami["cycle_started_at"] = profile.rehab_cycle_started_at
                await self._device.set_gamification(gami)
            except Exception as e:
                log.warning("[journey] failed to attach gamification: %r", e)

            # Persist initial session.json (status="running")
            self._patients.write_session(patient_name, session_id, self._snapshot_running())

            # ─── Sensing + Coordinator wiring ────────────────────────────
            # Make sure the session directory exists before sensing process
            # writes emotion_timeline.jsonl into it.
            session_dir = self._patients.create_session_dir(patient_name, session_id)
            get_coordinator().attach_session(session_dir)
            self._start_sensing(patient_name, plan)

            # Launch FSM task
            self._task = asyncio.create_task(
                self._run_session(),
                name=f"session-{session_id}",
            )

            log.info("Session started: patient=%s session=%s", patient_name, session_id)
            return {
                "session_id": session_id,
                "plan": plan.to_dict(),
                "estimated_duration_s": plan.total_session_s(),
            }

    # ---- Sensing lifecycle (private) -------------------------------

    def _start_sensing(self, patient_name: str, plan: Plan) -> None:
        """Spawn the sensing child process for this session."""
        if self._sensing_q is None:
            log.warning("No sensing_queue configured — skipping sensing process")
            return
        # Per-session stop event so we can cleanly join when the session ends
        self._sensing_stop = mp.Event()
        cfg = {
            "project_root": str(_PROJECT_ROOT),
            "subject":      patient_name,
            "dist_cm":      160,   # TODO Phase 5+: read from patient profile
            "baseline_min": plan.baseline_min,
        }
        try:
            self._sensing_proc = sensing_proc.start_sensing(
                cfg=cfg,
                status_q=self._sensing_q,
                stop_event=self._sensing_stop,
            )
            log.info("Sensing process started (pid=%s, name=%s)",
                     self._sensing_proc.pid, self._sensing_proc.name)
        except Exception as e:
            log.exception("Failed to start sensing process: %r", e)
            self._sensing_proc = None
            self._sensing_stop = None

    def _stop_sensing(self) -> None:
        """Signal sensing to stop and join the process (best-effort, blocking).

        Note: caller (`_finalize`) is async; the join here can block the
        event loop for up to 5s. In practice the sensing process responds to
        stop_event within ~500ms (mock) or ~1s (real). If this becomes a
        problem, wrap proc.join in `loop.run_in_executor`.
        """
        if self._sensing_stop is not None:
            try:
                self._sensing_stop.set()
            except Exception:
                pass
        if self._sensing_proc is not None:
            self._sensing_proc.join(timeout=5)
            if self._sensing_proc.is_alive():
                log.warning("Sensing process did not exit in 5s; terminating")
                self._sensing_proc.terminate()
                self._sensing_proc.join(timeout=2)
            self._sensing_proc = None
        self._sensing_stop = None
        get_coordinator().detach_session()

    async def _run_session(self) -> None:
        """The body of the session task — waits for FSM to finish, then finalizes."""
        result: SessionResult | None = None
        error: str | None = None
        try:
            result = await self._fsm.run()
        except asyncio.CancelledError:
            log.info("Session task cancelled")
            error = "cancelled"
            # Best-effort partial result
            if self._fsm:
                result = self._fsm.result()
            raise
        except Exception as e:
            log.exception("Session task failed: %r", e)
            error = repr(e)
            if self._fsm:
                result = self._fsm.result()
        finally:
            await self._finalize(result, error)

    async def _finalize(self, result: SessionResult | None, error: str | None) -> None:
        """Write final session.json and transition device to IDLE."""
        # Stop sensing FIRST so it stops appending to emotion_timeline.jsonl
        # before we finalize the session record.
        self._stop_sensing()

        if self._current_patient and self._current_session_id:
            final_snapshot = self._snapshot_final(result, error)
            self._patients.write_session(
                self._current_patient,
                self._current_session_id,
                final_snapshot,
            )
            log.info("Session persisted: %s/%s",
                     self._current_patient, self._current_session_id)

            # Phase 9: update the patient's Journey state (mountain ascent).
            # Returns a LedgerUpdate carrying any new milestones / title
            # upgrade; we attach this to the device_state so the screen can
            # play a celebration before the device returns to IDLE.
            ledger_update = await self._apply_journey_ledger(result)

            # Phase 6: enqueue AI assessment for this finished session.
            # Skips aborted-with-no-data sessions where there's nothing to assess.
            try:
                from .workers.ai_worker import enqueue_assessment
                completed = (result is not None and result.completed_reps > 0)
                if completed:
                    session_dir = self._patients.session_dir(
                        self._current_patient, self._current_session_id)
                    enqueue_assessment(session_dir, final_snapshot)
                else:
                    log.info("Skipping AI assessment (no completed reps)")
            except Exception as e:
                log.warning("Failed to enqueue AI assessment: %r", e)
        else:
            ledger_update = None

        # Broadcast a final celebration event before going IDLE, so the
        # screen can render the summary scene with the right rewards.
        if ledger_update is not None and ledger_update.has_celebration():
            try:
                await self._device.set_celebration(ledger_update.to_event_payload())
            except Exception as e:
                log.warning("Failed to set celebration: %r", e)

        # Return to IDLE
        await self._device.to_idle()

        # Reset state
        self._fsm = None
        self._task = None
        self._current_plan = None
        self._current_patient = None
        self._current_session_id = None
        self._session_started_at = 0.0

    # ---- Journey ledger -----------------------------------------

    async def _apply_journey_ledger(self, result: SessionResult | None):
        """
        Update the patient profile's Journey state after a session ends.

        Always swallows exceptions — a failure here must NOT prevent the
        device from returning to IDLE. Returns a LedgerUpdate on success,
        None on failure / no-op.
        """
        from .journey import apply_session as journey_apply
        if self._current_patient is None or result is None:
            return None
        try:
            profile = self._patients.get(self._current_patient)
        except Exception as e:
            log.warning("[ledger] cannot read profile %r: %r",
                        self._current_patient, e)
            return None
        upd = journey_apply(
            old_journey=profile.journey,
            completed_reps=result.completed_reps,
            cycle_weeks=profile.rehab_cycle_total_weeks,
        )
        try:
            patch = {"journey": upd.new_journey}
            # First-ever session also stamps the cycle start date for
            # the patient — the doctor sets the cycle length at creation
            # but the clock starts when training actually begins.
            if upd.is_new_journey and not profile.rehab_cycle_started_at:
                patch["rehab_cycle_started_at"] = upd.new_journey["last_session_date"]
            self._patients.update(self._current_patient, patch)
            log.info("[ledger] %s: +%dm → %dm  milestones=%s  title=%s",
                     self._current_patient, upd.elevation_gained,
                     upd.new_journey["total_elevation_m"],
                     upd.new_milestones,
                     upd.title_change[1] if upd.title_change else upd.new_journey["current_title"])
        except Exception as e:
            log.warning("[ledger] failed to persist journey: %r", e)
        return upd

    # ---- Control commands ----------------------------------------

    # Safety net: how long we wait for the FSM task to honor abort before
    # we forcibly cancel it. The FSM's _wait_in_phase wakes on the asyncio
    # Event within <10ms in practice; 3s gives generous headroom while
    # making sure a hung FSM never wedges the whole device.
    _ABORT_GRACE_S = 3.0

    async def stop(self) -> None:
        """Graceful stop — FSM marks abort, finalize writes whatever is done.

        Spawns a background watchdog that hard-cancels the session task if it
        doesn't honor the abort flag within _ABORT_GRACE_S. This is what
        guarantees the mobile UI's "终止" button always returns the device
        to IDLE, even if a future bug stalls the FSM loop."""
        if not self.is_running or not self._fsm:
            raise SessionManagerError("当前没有进行中的 session")
        log.info("[SessionManager] abort signalled (session=%s)",
                 self._current_session_id)
        self._fsm.abort()
        # Schedule watchdog; don't await here so the HTTP handler returns
        # immediately. The watchdog is fire-and-forget.
        asyncio.create_task(self._abort_watchdog(self._task),
                            name="abort-watchdog")

    async def _abort_watchdog(self, task: asyncio.Task | None) -> None:
        """If the session task is still alive after _ABORT_GRACE_S, cancel it.

        Cancelling forces the FSM out of any pending await; _run_session's
        finally still runs _finalize so persistence + state cleanup happen
        normally. The watchdog is no-op if the task already completed."""
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._ABORT_GRACE_S)
            log.info("[SessionManager] session task exited within grace")
        except asyncio.TimeoutError:
            log.warning("[SessionManager] session task did NOT exit in %.1fs — "
                        "force-cancelling", self._ABORT_GRACE_S)
            task.cancel()
        except asyncio.CancelledError:
            # The shielded task itself raised CancelledError; that's fine.
            pass
        except Exception as e:
            log.warning("[SessionManager] watchdog error: %r", e)

    async def abort(self) -> None:
        """Same as stop in Phase 2 (no data-discard difference yet)."""
        await self.stop()

    def pause(self) -> None:
        if not self.is_running or not self._fsm:
            raise SessionManagerError("当前没有进行中的 session")
        log.info("[SessionManager] pause (session=%s)", self._current_session_id)
        self._fsm.pause()

    def resume(self) -> None:
        if not self.is_running or not self._fsm:
            raise SessionManagerError("当前没有进行中的 session")
        log.info("[SessionManager] resume (session=%s)", self._current_session_id)
        self._fsm.resume()

    def skip_set(self) -> None:
        if not self.is_running or not self._fsm:
            raise SessionManagerError("当前没有进行中的 session")
        log.info("[SessionManager] skip_set (session=%s)", self._current_session_id)
        self._fsm.skip_set()

    # ---- Snapshots for persistence -------------------------------

    def _snapshot_running(self) -> dict:
        return {
            "session_id": self._current_session_id,
            "patient": self._current_patient,
            "start": _iso(self._session_started_at),
            "started_at_ts": self._session_started_at,
            "plan_used": self._current_plan.to_dict() if self._current_plan else None,
            "status": "running",
        }

    def _snapshot_final(self, result: SessionResult | None, error: str | None) -> dict:
        end = time.time()
        base = {
            "session_id": self._current_session_id,
            "patient": self._current_patient,
            "start": _iso(self._session_started_at),
            "end": _iso(end),
            "started_at_ts": self._session_started_at,
            "ended_at_ts": end,
            "duration_s": round(end - self._session_started_at, 2),
            "plan_used": self._current_plan.to_dict() if self._current_plan else None,
        }
        if result is not None:
            base.update(result.as_dict())
            base["status"] = "aborted" if result.aborted else "completed"
        else:
            base["status"] = "error"
        if error is not None:
            base["error"] = error
        return base


def _iso(ts: float) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ---- Module-level singleton accessor ---------------------------------

_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    if _manager is None:
        raise RuntimeError("SessionManager not initialized; call init_session_manager() first")
    return _manager


def init_session_manager(patients_dir: Path,
                          sensing_queue: mp.Queue | None = None) -> SessionManager:
    """Called once at app startup."""
    global _manager
    if _manager is None:
        _manager = SessionManager(
            device_state=get_device_manager(),
            patient_store=PatientStore(patients_dir),
            sensing_queue=sensing_queue,
        )
    return _manager


def get_patient_store() -> PatientStore:
    return get_session_manager()._patients
