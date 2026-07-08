"""
Coordinator — bridges sensing process events to:
  - DeviceStateManager (broadcasts emotion to screen + web)
  - Coach (rolling window analysis, trigger events)
  - Per-session JSONL logging (emotion_timeline + plan_adjustments)

Started once at backend startup (server.py lifespan). Lives for the lifetime
of the backend process. SessionManager calls attach_session() / detach_session()
to scope JSONL writes to the active session directory.

Why this lives in the main asyncio loop (not its own thread):
  - device_state mutations must serialize with HTTP handlers (asyncio.Lock)
  - WebSocket broadcasts are awaitable coroutines
  - ZMQ publish via zmq.asyncio is awaitable
All naturally async; only the mp.Queue.get() is blocking, so we run that
in the default executor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing as mp
import queue as _q
import time
from collections import deque
from pathlib import Path
from typing import Optional

from .coach import CoachState, evaluate as coach_evaluate, summarize_window
from .device_state import get_manager as get_device_manager

log = logging.getLogger(__name__)


# Keep this many recent inter-event timestamps for FPS calculation.
_FPS_WINDOW = 20
# Phase 10: stale-data detection. If these thresholds are crossed while a
# session is WORKING, the UI must show that data is stale instead of silently
# displaying the last sample as if it were live.
FRAME_STALE_S = 3.0
INFERENCE_STALE_S = 6.0


class Coordinator:
    """Single instance per backend process. Reads sensing events, fans out."""

    def __init__(self):
        self._device = get_device_manager()
        self._coach = CoachState()
        self._session_dir: Optional[Path] = None
        self._sample_count = 0     # how many inference events seen this session

        # Sensing health tracking (Phase 5)
        # Built incrementally as events flow in from sensing process.
        self._sensing: dict = {
            "mode": None,            # "real" | "mock" | None
            "running": False,
            "frames_total": 0,
            "fps_approx": 0.0,
            "baseline_pct": None,    # 0.0–1.0; None when sensing hasn't reported
            "baseline_done": False,
            "error": None,
            # Phase 10 diagnostics / stale detection
            "stale": False,
            "inference_stale": False,
            "last_frame_age_s": None,
            "last_inference_age_s": None,
            "engine_health": None,
        }
        # Timestamps of recent frame/inference events for FPS estimation.
        self._event_ts: deque = deque(maxlen=_FPS_WINDOW)
        # Throttle for outgoing emotion broadcasts on frame events.
        # Inference events (every ~2s) always broadcast; frame events
        # (now at radar's 10 Hz) only broadcast every ~0.5s.
        self._last_frame_broadcast_ts: float = 0.0
        # Phase 7: track last ambient label so we only call set_ambient on change
        self._last_ambient_label: str | None = None
        # Phase 10: timestamps for stale detection
        self._last_frame_ts: float = 0.0
        self._last_inference_ts: float = 0.0
        self._last_stale_broadcast_ts: float = 0.0

    # ---- Session lifecycle (called by SessionManager) -------------------

    def attach_session(self, session_dir: Path) -> None:
        """Start logging to a new session directory; reset Coach state."""
        self._session_dir = Path(session_dir)
        self._coach = CoachState()
        self._sample_count = 0
        # Reset sensing counters (mode/running set on sensing_started event)
        self._sensing = {
            "mode": None, "running": False, "frames_total": 0,
            "fps_approx": 0.0, "baseline_pct": None,
            "baseline_done": False, "error": None,
            "stale": False,
            "inference_stale": False,
            "last_frame_age_s": None,
            "last_inference_age_s": None,
            "engine_health": None,
        }
        self._event_ts.clear()
        self._last_ambient_label = None
        self._last_frame_ts = 0.0
        self._last_inference_ts = 0.0
        self._last_stale_broadcast_ts = 0.0
        log.info("Coordinator attached to session: %s", self._session_dir.name)

    def detach_session(self) -> None:
        """Stop logging. Coach state retained until next attach (idempotent)."""
        if self._session_dir is not None:
            log.info("Coordinator detached from session (%d samples logged)",
                     self._sample_count)
        self._session_dir = None
        # Phase 7: stop ambient when session ends
        try:
            from .audio_engine import get_engine
            eng = get_engine()
            if eng is not None:
                eng.set_ambient(None)
            self._last_ambient_label = None
        except Exception as e:
            log.warning("Audio stop on detach failed: %r", e)

    # ---- Main consumer loop --------------------------------------------

    async def run(self, status_q: mp.Queue, stop_event: asyncio.Event) -> None:
        """Run forever. Reads status_q, dispatches to handlers.

        Phase 10: on queue timeout, still run stale detection so the UI can
        flip to "data paused" even when *no* new radar events arrive.
        """
        log.info("Coordinator started")
        loop = asyncio.get_running_loop()

        while not stop_event.is_set():
            # mp.Queue.get is blocking, so do it in a thread pool slot.
            # Timeout 0.5s so we periodically re-check stop_event and run
            # stale detection even if the sensing process stops sending events.
            try:
                msg = await loop.run_in_executor(None, _q_get_with_timeout, status_q, 0.5)
            except Exception as e:
                log.warning("Coordinator queue read error: %r", e)
                await self._check_stale()
                continue
            if msg is None:
                await self._check_stale()
                continue

            try:
                await self._dispatch(msg)
                await self._check_stale()
            except Exception as e:
                log.exception("Coordinator dispatch error on %r: %r", msg.get("type"), e)
        log.info("Coordinator stopped")

    # ---- Per-event dispatch --------------------------------------------

    async def _dispatch(self, msg: dict) -> None:
        mtype = msg.get("type")

        if mtype == "inference":
            await self._on_inference(msg)
        elif mtype == "frame":
            await self._on_frame(msg)
        elif mtype == "baseline_progress":
            await self._on_baseline_progress(msg)
        elif mtype == "baseline_done":
            self._sensing["baseline_done"] = True
            self._sensing["baseline_pct"] = 1.0
            await self._device.update_sensing(dict(self._sensing))
            log.info("Sensing: baseline acquired")
        elif mtype == "engine_health":
            await self._on_engine_health(msg)
        elif mtype == "sensing_started":
            self._sensing["mode"] = msg.get("mode", "?")
            self._sensing["running"] = True
            self._sensing["error"] = None
            await self._device.update_sensing(dict(self._sensing))
            log.info("Sensing started (%s mode)", self._sensing["mode"])
        elif mtype == "sensing_stopped":
            self._sensing["running"] = False
            self._sensing["fps_approx"] = 0.0
            await self._device.update_sensing(dict(self._sensing))
            log.info("Sensing stopped")
        elif mtype == "sensing_error":
            err = msg.get("msg", "unknown")
            self._sensing["error"] = err
            self._sensing["running"] = False
            await self._device.update_sensing(dict(self._sensing))
            log.warning("Sensing error: %s", err)
        else:
            log.debug("Unknown sensing event: %r", mtype)

    def _record_event(self, ts: float) -> float:
        """Add a timestamp to the FPS window; return current FPS estimate."""
        self._event_ts.append(ts)
        if len(self._event_ts) < 2:
            return 0.0
        elapsed = self._event_ts[-1] - self._event_ts[0]
        if elapsed < 0.1:
            return 0.0
        return (len(self._event_ts) - 1) / elapsed

    async def _on_inference(self, msg: dict) -> None:
        self._sample_count += 1
        self._last_inference_ts = msg.get("ts") or time.time()
        self._sensing["inference_stale"] = False
        self._sensing["last_inference_age_s"] = 0.0
        label = msg.get("label")
        probs = msg.get("probs") or [0.0, 0.0, 0.0]
        br_bpm = msg.get("br_bpm")
        ts = msg.get("ts")

        # Note: don't bump frames_total here — frame events handle that.
        # Inference events fire only ~0.5 Hz (every 2s); they'd distort fps.

        # Build emotion snapshot — preserve chest_dist_cm from last frame event
        prior = self._device.get_status().get("emotion") or {}
        emotion_dict = {
            "label":         label,
            "probs":         probs,
            "br_bpm":        br_bpm,
            "chest_dist_cm": prior.get("chest_dist_cm"),
            "ts":            ts,
        }
        # Always broadcast on inference — it's the user-facing change
        await self._device.update_emotion_and_sensing(
            emotion=emotion_dict, sensing=dict(self._sensing))

        # Phase 7: switch ambient bed to match the current emotion.
        # Only on label CHANGE (not every inference) so the engine doesn't
        # restart the crossfade every 2s when label is stable.
        if label != self._last_ambient_label:
            try:
                from .audio_engine import get_engine
                eng = get_engine()
                if eng is not None:
                    eng.set_ambient(label)
                self._last_ambient_label = label
            except Exception as e:
                log.warning("Audio set_ambient failed: %r", e)

        # Coach evaluation (window-based trigger)
        trigger = coach_evaluate(self._coach, label, probs, ts)
        if trigger:
            await self._on_coach_trigger(trigger)

        # Log this sample
        self._append_jsonl("emotion_timeline.jsonl", {
            "ts": ts, "label": label, "probs": probs, "br_bpm": br_bpm,
        })

    async def _on_frame(self, msg: dict) -> None:
        # Frame events arrive at the radar's native ~10 Hz. We always count
        # them (for accurate FPS display) but throttle the broadcast to
        # ~2 Hz — high-frequency emotion-card updates burn CPU on the
        # screen (Chinese font render) and add no perceptual value to a
        # human watching distance/breathing.
        self._sensing["frames_total"] += 1
        now = msg.get("ts") or time.time()
        self._last_frame_ts = now
        self._sensing["stale"] = False
        self._sensing["last_frame_age_s"] = 0.0
        self._sensing["fps_approx"] = round(self._record_event(now), 1)

        # Broadcast throttle: every ~0.5s
        if now - self._last_frame_broadcast_ts < 0.5:
            return
        self._last_frame_broadcast_ts = now

        prior = self._device.get_status().get("emotion") or {}
        emotion_dict = dict(prior)
        if msg.get("br_bpm") is not None:
            emotion_dict["br_bpm"] = msg["br_bpm"]
        if msg.get("chest_dist_cm") is not None:
            emotion_dict["chest_dist_cm"] = msg["chest_dist_cm"]
        emotion_dict["ts"] = now
        await self._device.update_emotion_and_sensing(
            emotion=emotion_dict, sensing=dict(self._sensing))

    async def _on_baseline_progress(self, msg: dict) -> None:
        pct = float(msg.get("pct", 0))
        self._sensing["baseline_pct"] = pct
        await self._device.update_sensing(dict(self._sensing))
        # Cap log frequency
        if int(pct * 10) != int((pct - 0.01) * 10):
            log.debug("Baseline progress: %.0f%%", pct * 100)

    async def _on_engine_health(self, msg: dict) -> None:
        """Update low-level engine diagnostics from RehabEngine.

        This event is independent of UI frame/inference events, so it lets us
        diagnose whether the display is stale because SPI stopped, inference
        stopped, or only WebSocket/ZMQ stopped.
        """
        # Don't keep the redundant "type" field inside sensing.engine_health
        h = {k: v for k, v in msg.items() if k != "type"}
        self._sensing["engine_health"] = h
        if h.get("total_frames") is not None:
            self._sensing["frames_total"] = int(h.get("total_frames") or 0)
        if h.get("avg_fps") is not None:
            self._sensing["fps_approx"] = float(h.get("avg_fps") or 0.0)
        # If engine tells us its last frame/inference age, mirror those too.
        if h.get("spi_last_frame_age_s") is not None:
            self._sensing["last_frame_age_s"] = h.get("spi_last_frame_age_s")
            # Reconstruct timestamp so stale detection still works even if
            # only engine_health arrives but frame events are delayed/dropped.
            try:
                self._last_frame_ts = time.time() - float(h.get("spi_last_frame_age_s"))
            except Exception:
                pass
        if h.get("inference_last_age_s") is not None:
            self._sensing["last_inference_age_s"] = h.get("inference_last_age_s")
            try:
                self._last_inference_ts = time.time() - float(h.get("inference_last_age_s"))
            except Exception:
                pass
        await self._check_stale(force_broadcast=True)

    async def _check_stale(self, force_broadcast: bool = False) -> None:
        """Mark sensing as stale when frames or inference stop arriving.

        Called every queue timeout (~0.5s) and after each event. Broadcasts at
        most once per second unless forced by an engine_health event.
        """
        if not self._sensing.get("running"):
            return
        now = time.time()
        stale = False
        inf_stale = False
        if self._last_frame_ts:
            age = now - self._last_frame_ts
            self._sensing["last_frame_age_s"] = round(age, 1)
            stale = age > FRAME_STALE_S
        if self._last_inference_ts:
            iage = now - self._last_inference_ts
            self._sensing["last_inference_age_s"] = round(iage, 1)
            inf_stale = iage > INFERENCE_STALE_S
        # If no inference has ever happened after baseline, mark inference as
        # stale only after the session has had time to produce one.
        elif self._sensing.get("baseline_done") and self._last_frame_ts:
            iage = now - self._last_frame_ts
            self._sensing["last_inference_age_s"] = None
            inf_stale = iage > INFERENCE_STALE_S

        changed = (self._sensing.get("stale") != stale or
                   self._sensing.get("inference_stale") != inf_stale)
        self._sensing["stale"] = stale
        self._sensing["inference_stale"] = inf_stale
        if stale:
            self._sensing["error"] = "雷达数据暂停"
        elif inf_stale:
            self._sensing["error"] = "推理数据暂停"
        else:
            # Clear only stale-derived errors; keep real sensing_error messages.
            if self._sensing.get("error") in {"雷达数据暂停", "推理数据暂停"}:
                self._sensing["error"] = None

        should_broadcast = force_broadcast or changed or (now - self._last_stale_broadcast_ts > 1.0)
        if should_broadcast:
            self._last_stale_broadcast_ts = now
            await self._device.update_sensing(dict(self._sensing))

    async def _on_coach_trigger(self, trigger: dict) -> None:
        log.info("Coach trigger: %s (share %.0f%% over %.0fs)",
                 trigger["trigger"], trigger["share"] * 100, trigger["window_s"])
        self._append_jsonl("plan_adjustments.jsonl", trigger)

        # Phase 9: on sustained FRUSTRATION, open an empathy intercept.
        # The webapp shows a modal letting the patient pick their preferred
        # softening. Auto-clears after 30s if untouched (defaulting to
        # "continue"). The HDMI also reads device_state.empathy_request and
        # renders the "我看你今天有点累" scene.
        if trigger.get("label") == "frustration":
            req = {
                "ts":       trigger.get("ts"),
                "reason":   trigger["trigger"],
                "share":    trigger.get("share"),
                "options":  ["continue", "reduce_2", "rest_1m"],
            }
            try:
                await self._device.set_empathy_request(req)
                # Auto-clear after 30s so a forgotten modal doesn't linger
                async def _clear_after(seconds: float):
                    await asyncio.sleep(seconds)
                    # Only clear if STILL the same request (no choice made)
                    curr = self._device.get_status().get("empathy_request") or {}
                    if curr.get("ts") == req["ts"]:
                        await self._device.set_empathy_request(None)
                        log.info("[empathy] auto-cleared (no choice in 30s)")
                asyncio.create_task(_clear_after(30.0), name="empathy-clear")
            except Exception as e:
                log.warning("[empathy] failed to open intercept: %r", e)

        # Phase 6: hand off to LLM to generate a personalized phrase.
        # Phase 7: that text will be synthesized to wav and played through audio.
        try:
            from .workers.ai_worker import enqueue_encourage
            # Use the live emotion snapshot for richer context
            emo = self._device.get_status().get("emotion") or {}
            prog = self._device.get_status().get("progress") or {}
            enqueue_encourage({
                "ts":             trigger.get("ts"),
                "emotion_label":  trigger.get("label"),
                "emotion_prob":   trigger.get("share", 0.5),
                "current_set":    prog.get("current_set", 0),
                "total_sets":     prog.get("sets_total", 0),
                "current_rep":    prog.get("current_rep", 0),
                "total_reps":     prog.get("reps_total", 0),
                "trigger_reason": trigger["trigger"],
            })
        except Exception as e:
            log.warning("Failed to enqueue encourage: %r", e)

    # ---- Helpers -------------------------------------------------------

    def _append_jsonl(self, filename: str, record: dict) -> None:
        if self._session_dir is None:
            return
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            with (self._session_dir / filename).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("Failed to append %s: %r", filename, e)


def _q_get_with_timeout(q: mp.Queue, timeout: float):
    """Blocking helper for run_in_executor; returns None on timeout."""
    try:
        return q.get(timeout=timeout)
    except _q.Empty:
        return None


# ---- Singleton -----------------------------------------------------------

_instance: Coordinator | None = None


def get_coordinator() -> Coordinator:
    global _instance
    if _instance is None:
        _instance = Coordinator()
    return _instance
