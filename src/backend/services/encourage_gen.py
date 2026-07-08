"""
encourage_gen — generate one short encouragement line on Coach trigger.

Used by the Coordinator when Coach detects sustained frustration/pleasure.
Returns a short string (15-25 chars in Chinese). On LLM failure, falls back
to the predefined pool in prompts/encourage_fallback.txt.

Phase 6: text only. Phase 7 will pass this text to local TTS (MeloTTS) to
synthesize an audible wav for the audio engine to mix in.
"""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from pathlib import Path

from ..llm import factory
from ..llm.audit import log_call
from ..llm.client_base import LLMError
from ..llm.prompts import render_messages

log = logging.getLogger(__name__)

PROMPT_FILE   = "encourage_v1.md"
FALLBACK_FILE = (Path(__file__).resolve().parent.parent.parent
                 / "prompts" / "encourage_fallback.txt")
USE_CASE = "encourage"

# Per-process recent phrases (~5 entries). Avoid repetition within a session.
_recent: deque[str] = deque(maxlen=5)


async def generate_encourage_text(context: dict | None = None) -> str:
    """
    Generate one short encouragement line. Never raises — falls back on errors.

    context (all optional with defaults):
        emotion_label    "calm" | "frustration" | "pleasure"
        emotion_prob     float in [0,1]
        current_set/rep  int (1-based)
        total_sets/reps  int
        trigger_reason   "frustration_sustained" / ...
    """
    context = context or {}
    variables = {
        "emotion_label":   context.get("emotion_label", "calm"),
        "emotion_prob":    f"{float(context.get('emotion_prob', 0.8)):.2f}",
        "current_set":     context.get("current_set", 1),
        "total_sets":      context.get("total_sets", 3),
        "current_rep":     context.get("current_rep", 1),
        "total_reps":      context.get("total_reps", 12),
        "trigger_reason":  context.get("trigger_reason", "持续平静"),
        "recent_phrases":  " | ".join(_recent) if _recent else "(无)",
    }

    t0 = time.monotonic()
    try:
        messages = render_messages(PROMPT_FILE, variables)
        cfg = factory.get_config(USE_CASE)
        client = factory.get_client(USE_CASE)

        result = await client.chat(
            messages,
            temperature=cfg.get("temperature", 0.85),
            max_tokens=cfg.get("max_tokens", 80),
        )
        text = _clean(result.content)
        # Avoid repeating recent
        if text in _recent and len(_recent) >= 2:
            # One retry with slightly different inputs
            variables["trigger_reason"] = (variables["trigger_reason"]
                                            + " (请换个说法)")
            messages2 = render_messages(PROMPT_FILE, variables)
            try:
                result2 = await client.chat(messages2, temperature=0.95,
                                             max_tokens=cfg.get("max_tokens", 80))
                text2 = _clean(result2.content)
                if text2 and text2 != text:
                    text = text2
            except Exception:
                pass   # keep first text

        if not text:
            raise LLMError("LLM returned empty text")

        _recent.append(text)
        log_call(use_case=USE_CASE, model=result.model, prompt_v="v1",
                 prompt_tokens=result.prompt_tokens,
                 completion_tokens=result.completion_tokens,
                 latency_ms=result.latency_ms, status="ok",
                 cache_hit=result.cache_hit)
        return text

    except Exception as e:
        log.warning("[Encourage] LLM failed (%s); using fallback", e)
        log_call(use_case=USE_CASE, model="fallback", prompt_v="v1",
                 latency_ms=(time.monotonic() - t0) * 1000,
                 status="fallback", error=str(e))
        return _pick_fallback()


def _clean(text: str) -> str:
    """Strip whitespace, quotes, surrounding punctuation; cap length."""
    t = (text or "").strip()
    # Remove leading/trailing quotes
    for ch in ('"', "'", "“", "”", "‘", "’", "「", "」", "『", "』"):
        t = t.strip(ch)
    t = t.strip()
    # Single line only
    if "\n" in t:
        t = t.split("\n")[0].strip()
    # Hard cap (we asked LLM for 15-25 chars, but some overshoot)
    return t[:60] if t else ""


def _pick_fallback() -> str:
    """Random pick from the predefined wav-recorded phrase list."""
    if not FALLBACK_FILE.exists():
        return "做得不错,继续保持"
    try:
        lines = [ln.strip() for ln in
                 FALLBACK_FILE.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        if not lines:
            return "做得不错,继续保持"
        # Prefer phrases not recently used
        candidates = [p for p in lines if p not in _recent]
        if not candidates:
            candidates = lines
        pick = random.choice(candidates)
        _recent.append(pick)
        return pick
    except Exception:
        return "做得不错,继续保持"
