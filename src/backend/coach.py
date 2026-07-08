"""
Coach — analyzes the emotion stream and emits trigger events.

Phase 5 stance: **observable but not active**.
  - Computes "frustration_sustained" / "pleasure_sustained" triggers
  - Writes them to the session's plan_adjustments.jsonl
  - Surfaces them via device_state (UI can show "已检测到挫败")
  - Does NOT yet modify the running plan — that's Phase 6 (with LLM advice).

Why observe-only first: lets us tune trigger sensitivity against real users
without surprising them with mid-session plan changes. Once thresholds prove
right, Phase 6 wires Coach → SessionManager methods + LLM-generated TTS.

Algorithm:
  - Rolling 30s window of inference samples (label, probs)
  - A label triggers if it dominates the window with >50% share
  - 'calm' is the "neutral" state and never triggers
  - Per-label cooldown: 30s after triggering same label, suppress duplicates
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# ---- Tuning constants -----------------------------------------------------

EMOTION_WINDOW_S    = 30.0
DOMINANCE_THRESHOLD = 0.5     # >50% of window must be the same non-calm label
MIN_SAMPLES         = 5       # don't trigger on <5 samples (≈10s of data)
COOLDOWN_S          = 30.0    # per-label cooldown after firing
NEUTRAL_LABELS      = {"calm"}


@dataclass
class CoachState:
    """One per session; coordinator resets it on session start."""
    samples: deque = field(default_factory=lambda: deque(maxlen=120))
    last_trigger_ts: dict = field(default_factory=dict)


def evaluate(state: CoachState, label: str, probs: Optional[list],
             ts: Optional[float] = None) -> Optional[dict]:
    """
    Push a sample and check if any non-calm label dominates the window.

    Returns a trigger dict {ts, trigger, share, window_s, sample_count}
    or None if no trigger fires this tick.
    """
    ts = ts if ts is not None else time.time()
    state.samples.append((ts, label, probs))

    # Drop samples older than the window
    cutoff = ts - EMOTION_WINDOW_S
    while state.samples and state.samples[0][0] < cutoff:
        state.samples.popleft()

    if len(state.samples) < MIN_SAMPLES:
        return None

    # Tally labels in the window
    counts: dict[str, int] = {}
    for _, lbl, _ in state.samples:
        counts[lbl] = counts.get(lbl, 0) + 1
    total = len(state.samples)

    # Check each non-neutral label for dominance
    for lbl, n in counts.items():
        if lbl in NEUTRAL_LABELS:
            continue
        share = n / total
        if share < DOMINANCE_THRESHOLD:
            continue

        # Cooldown — don't re-fire same label too soon
        last = state.last_trigger_ts.get(lbl, 0.0)
        if ts - last < COOLDOWN_S:
            continue

        state.last_trigger_ts[lbl] = ts
        return {
            "ts":           ts,
            "trigger":      f"{lbl}_sustained",
            "label":        lbl,
            "share":        round(share, 3),
            "window_s":     EMOTION_WINDOW_S,
            "sample_count": total,
        }
    return None


def summarize_window(state: CoachState) -> dict:
    """Return a snapshot of current label distribution (for UI display)."""
    if not state.samples:
        return {"calm": 0.0, "frustration": 0.0, "pleasure": 0.0, "n": 0}
    counts: dict[str, int] = {}
    for _, lbl, _ in state.samples:
        counts[lbl] = counts.get(lbl, 0) + 1
    total = len(state.samples)
    out = {k: round(counts.get(k, 0) / total, 3)
           for k in ("calm", "frustration", "pleasure")}
    out["n"] = total
    return out
