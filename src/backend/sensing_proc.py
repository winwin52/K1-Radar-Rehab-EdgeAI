"""
Sensing process entry — runs as a child mp.Process.

Two modes share the same mp.Queue protocol:
  real_main:  imports rehab_engine, reads K1 SPI, runs RehabEngine
  mock_main:  generates synthetic emotion data (for Windows / dev / CI)

Mode selection:
  - explicit `mock` kwarg to start_sensing()
  - else: mock if sys.platform != 'linux' OR env REHAB_MOCK_SENSING=1
  - else: real

The parent backend does NOT import rehab_engine — it only imports this module.
That keeps the backend Windows-compatible (rehab_engine pulls in spidev/lgpio
which aren't installable on Windows).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import threading
import time


# ---- mp.Queue event protocol ----------------------------------------------
# All events have `type` and (mostly) `ts`. Sensible defaults below.
#
#   {"type": "sensing_started",  "mode": "real"|"mock"}
#   {"type": "sensing_stopped"}
#   {"type": "sensing_error", "msg": "..."}
#   {"type": "baseline_progress", "ts": float, "pct": float in [0, 1]}
#   {"type": "baseline_done",     "ts": float}
#   {"type": "inference",         "ts": float, "label": "calm"|"frustration"|"pleasure",
#                                  "probs": [calm, frus, plea], "br_bpm": float}
#   {"type": "engine_health",     "ts": float, "source": "spi"|"inference"|..., ...}


# ---- Real sensing (K1 only) -----------------------------------------------

def _real_main(cfg: dict, status_q: mp.Queue, stop_event) -> None:
    # Imported lazily so this module loads fine on Windows
    sys.path.insert(0, cfg["project_root"])
    from rehab_engine import RehabEngine

    engine = RehabEngine(
        subject=cfg.get("subject", "unknown"),
        dist_cm=cfg.get("dist_cm", 160),
        baseline_min=cfg.get("baseline_min", 4),
    )

    def _safe_put(item: dict) -> None:
        try:
            status_q.put_nowait(item)
        except Exception:
            pass  # drop on full queue; status updates are best-effort

    def on_frame(idx, elapsed, z_mean, emotion, probs, br_bpm, chest_cm):
        # No throttle — let the radar's real ~10 fps reach the UI. The
        # Coordinator decides whether to broadcast every frame or sample
        # down. A frame event is ~150 bytes; 10/s = ~1.5 KB/s, trivial.
        _safe_put({
            "type": "frame", "ts": time.time(),
            "frame_idx": idx, "elapsed_s": elapsed,
            "br_bpm": br_bpm, "chest_dist_cm": chest_cm,
            "motion_z": z_mean,
        })

    def on_inference(idx, features, probs, label, br_bpm):
        _safe_put({
            "type": "inference", "ts": time.time(),
            "frame_idx": idx, "label": label,
            "probs": list(probs) if probs is not None else None,
            "br_bpm": br_bpm,
        })

    def on_baseline_progress(pct: float):
        _safe_put({"type": "baseline_progress", "ts": time.time(), "pct": float(pct)})

    def on_baseline_done():
        _safe_put({"type": "baseline_done", "ts": time.time()})

    def on_health(h: dict):
        item = dict(h or {})
        item["type"] = "engine_health"
        item["ts"] = item.get("ts") or time.time()
        _safe_put(item)

    engine.on_frame             = on_frame
    engine.on_inference         = on_inference
    engine.on_baseline_progress = on_baseline_progress
    engine.on_baseline_done     = on_baseline_done
    engine.on_health            = on_health

    # Run engine.start() in a thread; main loop watches both stop_event and
    # whether the engine thread is still alive (so SPI errors propagate up).
    t = threading.Thread(target=engine.start, name="engine-run", daemon=True)
    t.start()

    _safe_put({"type": "sensing_started", "mode": "real"})

    try:
        while not stop_event.wait(0.5):
            if not t.is_alive():
                _safe_put({"type": "sensing_error", "msg": "engine thread terminated"})
                break
    finally:
        try:
            engine.stop()
        except Exception as e:
            print(f"[sensing/real] engine.stop() error: {e!r}", file=sys.stderr)
        t.join(timeout=3)
        _safe_put({"type": "sensing_stopped"})


# ---- Mock sensing (dev/test, any platform) --------------------------------

def _mock_main(cfg: dict, status_q: mp.Queue, stop_event) -> None:
    import random
    random.seed()

    def _safe_put(item: dict) -> None:
        try:
            status_q.put_nowait(item)
        except Exception:
            pass

    _safe_put({"type": "sensing_started", "mode": "mock"})

    # Phase 1: fake baseline progress
    baseline_s = cfg.get("baseline_min", 4) * 60.0
    if baseline_s > 0:
        steps = max(1, int(baseline_s // 2))  # tick every ~2s
        tick = baseline_s / steps
        for i in range(steps):
            if stop_event.wait(tick):
                _safe_put({"type": "sensing_stopped"})
                return
            _safe_put({
                "type": "baseline_progress", "ts": time.time(),
                "pct": (i + 1) / steps,
            })
    _safe_put({"type": "baseline_done", "ts": time.time()})

    # Phase 2: emit inference samples every ~2s with a drifting bias.
    # bias = [calm, frustration, pleasure] — sums to ~1; slowly drifts so the
    # UI shows interesting moves and Coach occasionally triggers.
    bias = [0.7, 0.15, 0.15]

    def _drift(bias_list):
        # Random walk in simplex
        idx = random.randrange(3)
        delta = random.uniform(-0.15, 0.15)
        bias_list[idx] = max(0.05, min(0.85, bias_list[idx] + delta))
        s = sum(bias_list)
        return [b / s for b in bias_list]

    labels = ("calm", "frustration", "pleasure")
    while not stop_event.is_set():
        # Pick a label by current bias
        label = random.choices(labels, weights=bias)[0]
        # Build a plausible probability vector that picks `label` as argmax
        probs = [random.uniform(0.05, 0.25) for _ in range(3)]
        idx = labels.index(label)
        probs[idx] = random.uniform(0.45, 0.9)
        s = sum(probs)
        probs = [round(p / s, 3) for p in probs]

        _safe_put({
            "type": "inference", "ts": time.time(),
            "label": label, "probs": probs,
            "br_bpm": round(random.uniform(13.0, 19.5), 1),
        })

        # Drift bias every few samples
        if random.random() < 0.15:
            bias = _drift(bias)

        # ~2s sleep with stop_event responsiveness
        for _ in range(20):
            if stop_event.is_set():
                break
            time.sleep(0.1)

    _safe_put({"type": "sensing_stopped"})


# ---- Mode selection / entrypoint ------------------------------------------

def is_mock_default() -> bool:
    """Mock unless on Linux and REHAB_MOCK_SENSING is unset/0."""
    if sys.platform != "linux":
        return True
    return os.environ.get("REHAB_MOCK_SENSING", "0") not in ("0", "", "false", "False")


def start_sensing(cfg: dict, status_q: mp.Queue, stop_event,
                  mock: bool | None = None) -> mp.Process:
    """
    Start a sensing child process. Returns the live Process.

    cfg expects:
        project_root: str    (only used in real mode)
        subject:      str
        dist_cm:      int
        baseline_min: int
    """
    if mock is None:
        mock = is_mock_default()
    target = _mock_main if mock else _real_main
    name = f"sensing-{'mock' if mock else 'real'}"
    proc = mp.Process(target=target, args=(cfg, status_q, stop_event),
                      name=name, daemon=False)
    proc.start()
    return proc
