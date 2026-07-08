"""
LLM call audit log — append one JSONL line per call.

Audit lives at audit/llm_calls.jsonl. Each line includes:
  - timestamp
  - use case (assessment / encourage / ...)
  - model name + prompt version
  - prompt / completion token counts
  - estimated cost (CNY)
  - latency
  - status (ok / fail / fallback)
  - session_id if applicable

The audit module never blocks business logic — exceptions during write are
swallowed (printed once to stderr).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from threading import Lock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_AUDIT_FILE   = _PROJECT_ROOT / "audit" / "llm_calls.jsonl"
_LOCK = Lock()
_WARNED = False


# Rough cost per token (CNY). Adjust when DeepSeek pricing changes.
# As of 2025: deepseek-chat = ¥1/M input + ¥2/M output (cache-miss)
_COST_TABLE: dict[str, tuple[float, float]] = {
    "deepseek-chat":     (1.0e-6, 2.0e-6),
    "deepseek-reasoner": (4.0e-6, 16.0e-6),
    "mock-v1":           (0.0,    0.0),
}


def log_call(
    *,
    use_case: str,
    model: str,
    prompt_v: str = "v1",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: float = 0.0,
    status: str = "ok",      # "ok" | "fail" | "fallback"
    cache_hit: bool = False,
    session_id: str | None = None,
    error: str | None = None,
) -> None:
    """Append one audit record. Never raises."""
    record = {
        "ts":               time.time(),
        "ts_iso":           time.strftime("%Y-%m-%dT%H:%M:%S"),
        "use_case":         use_case,
        "model":            model,
        "prompt_version":   prompt_v,
        "prompt_tokens":    prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens":     prompt_tokens + completion_tokens,
        "cost_yuan":        _estimate_cost(model, prompt_tokens, completion_tokens),
        "latency_ms":       round(latency_ms, 1),
        "status":           status,
        "cache_hit":        cache_hit,
        "session_id":       session_id,
        "error":            error,
    }
    try:
        with _LOCK:
            _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        global _WARNED
        if not _WARNED:
            print(f"[audit] write failed (suppressing future warnings): {e!r}",
                  file=sys.stderr)
            _WARNED = True


def _estimate_cost(model: str, prompt_tok: int, completion_tok: int) -> float:
    """Best-effort CNY cost estimate. Unknown model → assume deepseek-chat rates."""
    rates = _COST_TABLE.get(model.lower(), (1.0e-6, 2.0e-6))
    return round(prompt_tok * rates[0] + completion_tok * rates[1], 6)


def daily_summary() -> dict:
    """Return aggregate stats for today (from local-time midnight)."""
    if not _AUDIT_FILE.exists():
        return {"date": time.strftime("%Y-%m-%d"), "calls": 0,
                "total_tokens": 0, "cost_yuan": 0.0}
    today_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d 00:00:00"),
                                              "%Y-%m-%d %H:%M:%S"))
    calls = total_tokens = 0
    cost = 0.0
    try:
        for line in _AUDIT_FILE.open("r", encoding="utf-8"):
            try:
                r = json.loads(line)
                if r.get("ts", 0) >= today_start:
                    calls += 1
                    total_tokens += r.get("total_tokens", 0)
                    cost += r.get("cost_yuan", 0.0)
            except Exception:
                continue
    except Exception:
        pass
    return {
        "date":         time.strftime("%Y-%m-%d"),
        "calls":        calls,
        "total_tokens": total_tokens,
        "cost_yuan":    round(cost, 4),
    }
