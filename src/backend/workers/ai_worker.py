"""
AI background worker — single asyncio task consuming AI work items.

Task descriptors (dict):

  {"type": "assessment", "session_dir": str, "session_data": dict,
   "patient": str, "session_id": str}

  {"type": "encourage", "context": dict}

The worker keeps running across sessions; the queue is global to the backend.

Failure handling:
  - LLMRetryableError → persist task to queue/ai_retry.jsonl, retry on next startup
  - LLMFatalError      → log and drop
  - Other exceptions   → log, persist to retry queue (best-effort)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ..llm.client_base import LLMFatalError, LLMRetryableError
from ..services.ai_assessment import generate_assessment

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RETRY_FILE   = _PROJECT_ROOT / "queue" / "ai_retry.jsonl"


# ---- Module-level queue (singleton) ----------------------------------

_queue: asyncio.Queue | None = None


def get_queue() -> asyncio.Queue | None:
    return _queue


def enqueue_assessment(session_dir: Path, session_data: dict) -> bool:
    """Push an assessment task. Non-blocking; returns False if dropped."""
    if _queue is None:
        log.warning("[AI Worker] not initialized; assessment dropped")
        return False
    try:
        _queue.put_nowait({
            "type":         "assessment",
            "session_dir":  str(session_dir),
            "session_data": session_data,
        })
        log.info("[AI Worker] enqueued assessment for %s",
                 session_data.get("session_id"))
        return True
    except asyncio.QueueFull:
        log.warning("[AI Worker] queue full; assessment dropped")
        return False


def enqueue_encourage(context: dict) -> bool:
    if _queue is None:
        return False
    try:
        _queue.put_nowait({"type": "encourage", "context": context})
        return True
    except asyncio.QueueFull:
        return False


# ---- Worker loop -----------------------------------------------------

async def run_worker(stop: asyncio.Event) -> None:
    """Main worker coroutine. Started by backend lifespan."""
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=100)

    # Replay any tasks left from a previous run
    await _replay_retry_queue()

    log.info("[AI Worker] started")
    while not stop.is_set():
        try:
            task = await asyncio.wait_for(_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        try:
            await _handle(task)
        except LLMFatalError as e:
            log.error("[AI Worker] fatal LLM error on %s: %s", task.get("type"), e)
            # Don't retry fatal errors
        except LLMRetryableError as e:
            log.warning("[AI Worker] retryable error on %s: %s — persisted for retry",
                        task.get("type"), e)
            _persist_retry(task)
        except Exception as e:
            log.exception("[AI Worker] unexpected error on %s: %r",
                          task.get("type"), e)
            _persist_retry(task)
    log.info("[AI Worker] stopped")


async def _handle(task: dict) -> None:
    ttype = task.get("type")
    if ttype == "assessment":
        sd = Path(task["session_dir"])
        await generate_assessment(sd, task["session_data"])
    elif ttype == "encourage":
        from ..services.encourage_gen import generate_encourage_text
        text = await generate_encourage_text(task.get("context"))
        # Phase 6: just log. Phase 7 will hand off to TTS / audio.
        log.info("[AI Worker] encourage: %r", text)
        # Also store on device_state so UI can show it
        try:
            from ..device_state import get_manager
            mgr = get_manager()
            current = mgr.get_status().get("emotion") or {}
            current = dict(current)
            current["coach_text"] = text
            current["coach_ts"]   = task.get("context", {}).get("ts")
            await mgr.update_emotion(current)
        except Exception as e:
            log.warning("[AI Worker] failed to surface encourage text to UI: %r", e)
    else:
        log.warning("[AI Worker] unknown task type: %r", ttype)


# ---- Retry queue persistence ----------------------------------------

async def _replay_retry_queue() -> None:
    """Re-enqueue any tasks left from previous runs (medical data is precious)."""
    if not _RETRY_FILE.exists():
        return
    try:
        lines = _RETRY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return

    # Clear file before re-enqueuing — new failures get fresh appends
    try:
        _RETRY_FILE.unlink()
    except Exception:
        pass

    replayed = 0
    for line in lines:
        try:
            task = json.loads(line)
            if _queue is not None:
                await _queue.put(task)
                replayed += 1
        except Exception:
            continue
    if replayed:
        log.info("[AI Worker] replayed %d task(s) from retry queue", replayed)


def _persist_retry(task: dict) -> None:
    """Append failed task to retry queue for next-startup replay."""
    try:
        _RETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _RETRY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(task, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.warning("[AI Worker] failed to persist retry task: %r", e)
