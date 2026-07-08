"""
SessionFSM — drives one rehabilitation session through its lifecycle.

States (the `fsm_state` string published to device_state.sub_state):
    BASELINE                 静坐采集基线 (baseline_min × 60 秒)
    TRAINING.REP_LIFT        抬腿
    TRAINING.REP_HOLD        保持
    TRAINING.REP_LOWER       放下
    TRAINING.REP_REST        次间间歇
    TRAINING.SET_REST        组间休息
    SUMMARY                  完成总结 (用户可补备注;等待 finalize)
    ABORTED                  强制中止终态
    PAUSED                   用户暂停 (沿用上一个 sub_state 显示)

`progress` payload pushed alongside each state change:
    {
      "current_set": int,    1-based, 0 during BASELINE
      "current_rep": int,    1-based, 0 outside REP_*
      "sets_total": int,
      "reps_total": int,
      "countdown_s": float,  remaining seconds in current sub-phase
      "phase_total_s": float, total seconds of current sub-phase
      "paused": bool
    }

The FSM is a single asyncio.Task. Control points (skip_set / pause / abort)
are non-blocking flags checked on each tick — no callbacks reach into the FSM
synchronously, so there's no need for locks.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .plan import Plan


# ---- State constants ------------------------------------------------

S_BASELINE   = "BASELINE"
S_REP_LIFT   = "TRAINING.REP_LIFT"
S_REP_HOLD   = "TRAINING.REP_HOLD"
S_REP_LOWER  = "TRAINING.REP_LOWER"
S_REP_REST   = "TRAINING.REP_REST"
S_SET_REST   = "TRAINING.SET_REST"
S_SUMMARY    = "SUMMARY"
S_ABORTED    = "ABORTED"

# How often we publish progress while waiting in a phase (seconds)
PUBLISH_INTERVAL_S = 0.5


# ---- Callback signature ---------------------------------------------

# Called by FSM on every progress publish: (fsm_state, progress_dict).
ProgressCallback = Callable[[str, dict], Awaitable[None]]

# Called when FSM transitions terminally (completed / aborted).
# Argument is the final result dict (see make_result()).
FinishCallback = Callable[[dict], Awaitable[None]]


# ---- Result dataclass -----------------------------------------------

@dataclass
class SessionResult:
    completed: bool
    aborted: bool
    completed_sets: int
    completed_reps: int       # total reps across all sets
    planned_total_reps: int
    duration_s: float
    sets_history: list[dict] = field(default_factory=list)
    # sets_history entry: {set_idx, reps_done, started_at, ended_at}

    def completion_pct(self) -> float:
        if self.planned_total_reps == 0:
            return 0.0
        return round(100.0 * self.completed_reps / self.planned_total_reps, 1)

    def as_dict(self) -> dict:
        return {
            "completed": self.completed,
            "aborted": self.aborted,
            "completed_sets": self.completed_sets,
            "completed_reps": self.completed_reps,
            "planned_total_reps": self.planned_total_reps,
            "duration_s": round(self.duration_s, 2),
            "completion_pct": self.completion_pct(),
            "sets_history": self.sets_history,
        }


# ---- FSM ------------------------------------------------------------

class SessionFSM:
    """
    Runs one session to completion or until aborted.

    Lifecycle:
        fsm = SessionFSM(plan, patient, session_id, on_progress)
        await fsm.run()    # blocks until completed / cancelled
        result = fsm.result()
    """

    def __init__(
        self,
        plan: Plan,
        patient: str,
        session_id: str,
        on_progress: ProgressCallback,
    ):
        self.plan = plan
        self.patient = patient
        self.session_id = session_id
        self._on_progress = on_progress

        # Control flags (atomic Python assignment; no lock needed)
        self._paused = False
        self._skip_set = False     # consumed at next opportunity
        self._abort = False

        # asyncio.Event used to wake the wait-in-phase loop the instant
        # pause/resume/skip/abort fires. Without this, UI feedback can lag
        # by up to PUBLISH_INTERVAL_S (0.5s) — long enough that users think
        # the button didn't work.
        self._wake = asyncio.Event()

        # Live counters (driven from inside _run)
        self._current_set = 0      # 1-based once training starts
        self._current_rep = 0
        self._sets_history: list[dict] = []
        self._started_at = 0.0
        self._ended_at = 0.0

    # ---- Public control --------------------------------------------

    def pause(self) -> None:
        self._paused = True
        self._wake.set()      # interrupt any in-flight sleep

    def resume(self) -> None:
        self._paused = False
        self._wake.set()      # interrupt the pause-spin

    def skip_set(self) -> None:
        """Skip the remainder of the current set; one-shot, auto-cleared."""
        self._skip_set = True
        self._wake.set()

    def abort(self) -> None:
        """Stop ASAP, persist whatever was completed."""
        self._abort = True
        self._paused = False   # un-pause so the loop can see _abort
        self._wake.set()

    def is_paused(self) -> bool:
        return self._paused

    def is_aborting(self) -> bool:
        return self._abort

    # ---- Result ----------------------------------------------------

    def result(self) -> SessionResult:
        completed_reps = sum(s.get("reps_done", 0) for s in self._sets_history)
        return SessionResult(
            completed=not self._abort and self._current_set >= self.plan.sets,
            aborted=self._abort,
            completed_sets=len([s for s in self._sets_history
                                if s.get("reps_done", 0) == self.plan.reps_per_set]),
            completed_reps=completed_reps,
            planned_total_reps=self.plan.sets * self.plan.reps_per_set,
            duration_s=(self._ended_at or time.time()) - self._started_at if self._started_at else 0.0,
            sets_history=self._sets_history,
        )

    # ---- Main loop -------------------------------------------------

    async def run(self) -> SessionResult:
        self._started_at = time.time()
        try:
            await self._stage_baseline()
            if self._abort:
                return self.result()
            await self._stage_training()
            if not self._abort:
                await self._publish(S_SUMMARY, {
                    "current_set": self.plan.sets,
                    "current_rep": 0,
                    "sets_total": self.plan.sets,
                    "reps_total": self.plan.reps_per_set,
                    "countdown_s": 0.0,
                    "phase_total_s": 0.0,
                    "paused": False,
                })
        except asyncio.CancelledError:
            self._abort = True
            raise
        finally:
            self._ended_at = time.time()
        return self.result()

    # ---- Stages ----------------------------------------------------

    async def _stage_baseline(self) -> None:
        duration = self.plan.baseline_min * 60.0
        await self._wait_in_phase(
            S_BASELINE,
            duration,
            extras_fn=lambda remaining: {
                "current_set": 0,
                "current_rep": 0,
                "sets_total": self.plan.sets,
                "reps_total": self.plan.reps_per_set,
            },
        )

    async def _stage_training(self) -> None:
        for set_idx in range(self.plan.sets):
            if self._abort:
                return
            self._current_set = set_idx + 1
            set_start = time.time()
            reps_done = 0

            for rep_idx in range(self.plan.reps_per_set):
                if self._abort:
                    break
                if self._skip_set:
                    self._skip_set = False
                    break
                self._current_rep = rep_idx + 1
                await self._do_rep()
                if self._abort or self._skip_set:
                    # Don't count a rep we didn't finish
                    if self._skip_set:
                        self._skip_set = False
                        break
                    break
                reps_done = rep_idx + 1

            self._sets_history.append({
                "set_idx": set_idx + 1,
                "reps_done": reps_done,
                "reps_planned": self.plan.reps_per_set,
                "started_at": set_start,
                "ended_at": time.time(),
            })
            self._current_rep = 0

            # Inter-set rest (skip after the last set)
            is_last_set = (set_idx == self.plan.sets - 1)
            if not is_last_set and not self._abort:
                await self._wait_in_phase(
                    S_SET_REST,
                    float(self.plan.rest_between_set_s),
                    extras_fn=lambda r: {
                        "current_set": self._current_set,
                        "current_rep": 0,
                        "sets_total": self.plan.sets,
                        "reps_total": self.plan.reps_per_set,
                    },
                )

    async def _do_rep(self) -> None:
        """One full rep: lift → hold → lower → rest. Cleanly abortable."""
        phases = [
            (S_REP_LIFT,  self.plan.lift_s,  "lift"),
            (S_REP_HOLD,  self.plan.hold_s,  "hold"),
            (S_REP_LOWER, self.plan.lower_s, "lower"),
            (S_REP_REST,  self.plan.rest_between_rep_s, "rest"),
        ]
        for state, dur, cue_name in phases:
            if self._abort or self._skip_set:
                return
            # Phase 7: trigger the one-shot rep cue at phase start
            self._fire_cue(cue_name)
            await self._wait_in_phase(
                state, dur,
                extras_fn=lambda r: {
                    "current_set": self._current_set,
                    "current_rep": self._current_rep,
                    "sets_total": self.plan.sets,
                    "reps_total": self.plan.reps_per_set,
                },
            )

    # ---- The wait primitive ---------------------------------------

    async def _wait_in_phase(self, fsm_state: str, duration_s: float,
                             extras_fn) -> None:
        """
        Block in `fsm_state` for `duration_s` seconds of NON-PAUSED wall-clock,
        publishing progress at PUBLISH_INTERVAL_S.

        Honors pause (extends wall-clock; doesn't consume remaining),
        skip_set (returns early), and abort (returns early).

        Uses `_wake` (asyncio.Event) so pause/resume/skip/abort take effect
        within milliseconds instead of waiting for the next sleep tick.
        """
        accumulated = 0.0
        while accumulated < duration_s:
            if self._abort or self._skip_set:
                return

            currently_paused = self._paused
            remaining = duration_s - accumulated

            extras = extras_fn(remaining)
            extras["countdown_s"]    = round(remaining, 1)
            extras["phase_total_s"]  = round(duration_s, 1)
            extras["paused"]         = currently_paused
            await self._publish(fsm_state, extras)

            if currently_paused:
                # Wait for resume/skip/abort, OR for PUBLISH_INTERVAL_S to
                # re-publish (so UI's countdown timestamp doesn't go stale).
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                            timeout=PUBLISH_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
                continue

            # Not paused: sleep but break early if pause/abort/skip fires.
            step = min(PUBLISH_INTERVAL_S, remaining)
            t_start = time.monotonic()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=step)
                actual = time.monotonic() - t_start
                # Woken by event — pause/skip/abort happened; loop will react
            except asyncio.TimeoutError:
                actual = step

            # Only count time that wasn't paused. If pause was requested
            # mid-sleep, we DON'T accumulate that fragment (conservative).
            if not self._paused:
                accumulated += actual

        # Final 0s publish so the UI knows the phase ended.
        extras = extras_fn(0.0)
        extras["countdown_s"]   = 0.0
        extras["phase_total_s"] = round(duration_s, 1)
        extras["paused"]        = False
        await self._publish(fsm_state, extras)

    async def _publish(self, fsm_state: str, progress: dict) -> None:
        try:
            await self._on_progress(fsm_state, progress)
        except Exception as e:
            # Never let publisher failures kill the FSM
            print(f"[SessionFSM] publish error: {e!r}")

    def _fire_cue(self, name: str) -> None:
        """Fire-and-forget audio cue. Best-effort; never raises."""
        try:
            from .audio_engine import get_engine
            eng = get_engine()
            if eng is not None:
                eng.play_cue(name)
        except Exception:
            pass
