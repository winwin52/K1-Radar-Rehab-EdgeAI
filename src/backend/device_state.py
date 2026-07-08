"""
Device state management.

K1 is an embedded device with two top-level states:
  IDLE     — default; backend running, sensing/audio not active, waits for command
  WORKING  — a rehab session is in progress (sub-state tracks FSM internally)
  ERROR    — sensor lost / config invalid / etc.

The device-level state never changes between BASELINE/TRAINING/SUMMARY —
those are sub-states under WORKING, tracked here as `sub_state`.

State changes are async-broadcast to subscribers (ZMQ to screen, WebSocket to web).
All transitions go through a single asyncio.Lock to keep them serialized.
"""

import asyncio
import time
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Callable, Awaitable


class DeviceState(str, Enum):
    IDLE = "IDLE"
    WORKING = "WORKING"
    ERROR = "ERROR"


@dataclass
class DeviceStatus:
    state: DeviceState = DeviceState.IDLE
    sub_state: str | None = None
    session_id: str | None = None
    patient: str | None = None
    started_at: float | None = None
    error_msg: str | None = None
    last_change_ts: float = field(default_factory=time.time)
    # Phase 2: rich session progress (current rep/set, countdown, etc.)
    progress: dict | None = None
    # Phase 5: live emotion / physiology snapshot
    #   {label, probs, br_bpm, chest_dist_cm, ts}
    emotion: dict | None = None
    # Phase 5: radar sensing health snapshot
    #   {mode, running, frames_total, fps_approx, baseline_pct, baseline_done, error}
    sensing: dict | None = None
    # Phase 9: gamification snapshot for the patient currently in WORKING
    #   {elevation_m, target_m, progress_pct, title, stage, streak_days, ...}
    # Set by SessionManager.start (from patient profile.journey). Cleared
    # in to_idle. Doesn't change during the session (the live elevation
    # gained is tracked via `progress.current_rep` * ELEVATION_PER_REP at
    # render time, not as a state mutation).
    gamification: dict | None = None
    # Phase 9: one-shot celebration after a session ends, e.g.
    #   {elevation_gained: 180, new_milestones: [500], title_change: ["...","..."]}
    # The screen reads this in the summary scene and plays an animation;
    # cleared on next to_working.
    celebration: dict | None = None
    # Phase 9: empathy intercept — set by Coordinator when sustained
    # frustration is detected. Webapp shows a modal with options:
    #   {ts, options: ["continue","reduce_2","rest_1m"], chosen: null|str}
    # Patient picks on the phone (HDMI is display-only); SessionManager
    # consumes the choice via /api/session/empathy_choice and clears this.
    empathy_request: dict | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "sub_state": self.sub_state,
            "session_id": self.session_id,
            "patient": self.patient,
            "started_at": self.started_at,
            "error_msg": self.error_msg,
            "last_change_ts": self.last_change_ts,
            "progress": self.progress,
            "emotion": self.emotion,
            "sensing": self.sensing,
            # Phase 9
            "gamification": self.gamification,
            "celebration": self.celebration,
            "empathy_request": self.empathy_request,
            "now": time.time(),
        }


# Async callback receiving a snapshot of the current status
StateChangeCallback = Callable[[DeviceStatus], Awaitable[None]]


class StateTransitionError(Exception):
    """Raised when an invalid state transition is attempted (e.g. start while WORKING)."""


class DeviceStateManager:
    """
    Single source of truth for device state.

    Read with get_status() (cheap, no lock); mutate via transition methods
    (each acquires asyncio.Lock then broadcasts to subscribers).
    """

    def __init__(self):
        self._status = DeviceStatus()
        self._lock = asyncio.Lock()
        self._subscribers: list[StateChangeCallback] = []

    # ---- Subscriptions --------------------------------------------------

    def subscribe(self, callback: StateChangeCallback) -> None:
        self._subscribers.append(callback)

    async def _notify(self) -> None:
        # snapshot a copy so subscribers can't mutate our state via shared refs
        snapshot = DeviceStatus(**asdict(self._status))
        snapshot.state = DeviceState(snapshot.state)  # asdict serializes enum to str
        for cb in list(self._subscribers):
            try:
                await cb(snapshot)
            except Exception as e:
                print(f"[DeviceState] subscriber error: {e!r}")

    # ---- Read -----------------------------------------------------------

    def get_status(self) -> dict:
        return self._status.as_dict()

    def is_idle(self) -> bool:
        return self._status.state == DeviceState.IDLE

    def is_working(self) -> bool:
        return self._status.state == DeviceState.WORKING

    # ---- Transitions ---------------------------------------------------

    async def to_working(self, session_id: str, patient: str) -> None:
        """IDLE → WORKING (raises StateTransitionError if not in IDLE)."""
        async with self._lock:
            if self._status.state != DeviceState.IDLE:
                raise StateTransitionError(
                    f"Cannot start session: device is {self._status.state.value}, "
                    f"expected IDLE"
                )
            self._status = DeviceStatus(
                state=DeviceState.WORKING,
                sub_state="BASELINE",
                session_id=session_id,
                patient=patient,
                started_at=time.time(),
                last_change_ts=time.time(),
            )
        await self._notify()

    async def set_gamification(self, gamification: dict | None) -> None:
        """Attach the patient's Journey snapshot to the device state.

        Called by SessionManager.start right after to_working so the screen
        and webapp can render the mountain at the correct starting point.
        No-op if not WORKING (we don't want stale journey data in IDLE)."""
        async with self._lock:
            if self._status.state != DeviceState.WORKING:
                return
            self._status.gamification = gamification
            self._status.last_change_ts = time.time()
        await self._notify()

    async def set_celebration(self, celebration: dict | None) -> None:
        """One-shot reward payload, consumed by the summary scene."""
        async with self._lock:
            self._status.celebration = celebration
            self._status.last_change_ts = time.time()
        await self._notify()

    async def set_empathy_request(self, request: dict | None) -> None:
        """Open or close a sustained-frustration intercept.

        Pass None to clear (after the patient picked or it timed out).
        Coordinator opens it; SessionManager closes it after the choice
        endpoint runs."""
        async with self._lock:
            self._status.empathy_request = request
            self._status.last_change_ts = time.time()
        await self._notify()

    async def update_sub_state(self, sub_state: str) -> None:
        """Update WORKING.sub_state (BASELINE/TRAINING/SUMMARY/...). No-op if not WORKING."""
        async with self._lock:
            if self._status.state != DeviceState.WORKING:
                return
            self._status.sub_state = sub_state
            self._status.last_change_ts = time.time()
        await self._notify()

    async def update_progress(self, sub_state: str | None,
                              progress: dict | None) -> None:
        """
        Update sub_state and progress together — atomic broadcast.

        Used by SessionFSM to push fine-grained state (current rep/set, countdown)
        without spawning multiple broadcasts per tick.

        No-op if device is not WORKING (defensive: prevents FSM from leaking
        progress into IDLE state if it cancels late).
        """
        async with self._lock:
            if self._status.state != DeviceState.WORKING:
                return
            if sub_state is not None:
                self._status.sub_state = sub_state
            self._status.progress = progress
            self._status.last_change_ts = time.time()
        await self._notify()

    async def update_emotion(self, emotion: dict | None) -> None:
        """Update emotion snapshot (Phase 5+). Doesn't change sub_state."""
        async with self._lock:
            if self._status.state != DeviceState.WORKING:
                return
            self._status.emotion = emotion
            # Don't bump last_change_ts; emotion is high-frequency and
            # consumers can filter by it if needed.
        await self._notify()

    async def update_sensing(self, sensing: dict | None) -> None:
        """Update radar sensing health snapshot. Survives across WORKING/IDLE
        so users can see "sensing stopped" briefly after a session ends.
        Pass None to clear."""
        async with self._lock:
            self._status.sensing = sensing
        await self._notify()

    async def update_emotion_and_sensing(self, emotion: dict | None,
                                          sensing: dict | None) -> None:
        """Atomic update of both — one broadcast per call.

        Use this from the Coordinator's frame/inference handlers so subscribers
        receive a single coherent snapshot.
        """
        async with self._lock:
            if self._status.state == DeviceState.WORKING:
                self._status.emotion = emotion
            self._status.sensing = sensing
        await self._notify()

    async def to_idle(self) -> None:
        """Any → IDLE (used for normal end and abort; data persistence happens before).

        Phase 9: preserves `celebration` and `sensing` across the transition.
        - celebration: the idle screen still shows "你刚完成了一段路程" until
          the next session starts (to_working clears it via fresh DeviceStatus).
        - sensing: a brief "stopped" status is useful for diagnostics."""
        async with self._lock:
            preserved_celebration = self._status.celebration
            preserved_sensing = self._status.sensing
            self._status = DeviceStatus(
                state=DeviceState.IDLE,
                last_change_ts=time.time(),
                celebration=preserved_celebration,
                sensing=preserved_sensing,
            )
        await self._notify()

    async def to_error(self, msg: str) -> None:
        """Any → ERROR (sensor lost, fatal config etc.)."""
        async with self._lock:
            self._status.state = DeviceState.ERROR
            self._status.error_msg = msg
            self._status.last_change_ts = time.time()
        await self._notify()


# ---- Singleton --------------------------------------------------------

_manager: DeviceStateManager | None = None


def get_manager() -> DeviceStateManager:
    global _manager
    if _manager is None:
        _manager = DeviceStateManager()
    return _manager
