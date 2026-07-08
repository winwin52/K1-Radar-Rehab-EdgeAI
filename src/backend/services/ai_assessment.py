"""
ai_assessment — generate the AI rehab assessment report for a finished session.

Pipeline:
  1. Read session.json (final state) + emotion_timeline.jsonl + plan_adjustments
  2. Compute aggregates (emotion %, BR mean/peak, completion %)
  3. Sanitize (hash patient name)
  4. Render prompts/assessment_v1.md
  5. Call LLM (DeepSeek or mock)
  6. Write ai_assessment.md to the session directory
  7. Log audit record

On failure: raise. Caller (ai_worker) handles retry queueing.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..llm import factory
from ..llm.audit import log_call
from ..llm.client_base import LLMError
from ..llm.prompts import render_messages
from ..llm.sanitizer import hash_patient_id

log = logging.getLogger(__name__)

PROMPT_FILE = "assessment_v1.md"
USE_CASE = "assessment"


async def generate_assessment(session_dir: Path, session_data: dict) -> str:
    """
    Generate ai_assessment.md and return its content.

    `session_dir`     — patients/<name>/sessions/<id>/
    `session_data`    — the final session.json dict
    """
    session_dir = Path(session_dir)
    patient_name = session_data.get("patient", "anonymous")
    session_id   = session_data.get("session_id")
    log.info("[Assessment] start: %s / %s", patient_name, session_id)

    # ---- Build prompt variables ----------------------------------
    variables = _build_variables(session_dir, session_data, patient_name)

    # ---- Render prompt + call LLM -------------------------------
    messages = render_messages(PROMPT_FILE, variables)
    cfg = factory.get_config(USE_CASE)
    client = factory.get_client(USE_CASE)

    t0 = time.monotonic()
    try:
        result = await client.chat(
            messages,
            temperature=cfg.get("temperature", 0.3),
            max_tokens=cfg.get("max_tokens", 2000),
        )
    except LLMError as e:
        log_call(use_case=USE_CASE, model="unknown", prompt_v="v1",
                 latency_ms=(time.monotonic() - t0) * 1000,
                 status="fail", error=str(e), session_id=session_id)
        raise

    log_call(use_case=USE_CASE, model=result.model, prompt_v="v1",
             prompt_tokens=result.prompt_tokens,
             completion_tokens=result.completion_tokens,
             latency_ms=result.latency_ms, status="ok",
             cache_hit=result.cache_hit, session_id=session_id)

    # ---- Write to disk ------------------------------------------
    out_file = session_dir / "ai_assessment.md"
    try:
        out_file.write_text(result.content, encoding="utf-8")
    except Exception as e:
        log.exception("Failed to write %s: %r", out_file, e)
        raise

    log.info("[Assessment] done: %s (model=%s, %d completion tokens, %.0fms)",
             out_file.name, result.model, result.completion_tokens,
             result.latency_ms)
    return result.content


# ---- Variable construction ---------------------------------------

def _build_variables(session_dir: Path, session: dict, patient_name: str) -> dict:
    plan_used = session.get("plan_used") or {}
    emotion_pcts = _emotion_distribution(session_dir)
    return {
        "patient_id":     hash_patient_id(patient_name),
        "session_date":   session.get("start", "unknown"),
        "duration_min":   round(session.get("duration_s", 0) / 60, 1),
        "baseline_quality": _baseline_quality(plan_used.get("baseline_min", 4)),

        "plan_sets":   plan_used.get("sets", 0),
        "plan_reps":   plan_used.get("reps_per_set", 0),
        "plan_hold_s": plan_used.get("hold_s", 0),

        "actual_sets":       session.get("completed_sets", 0),
        "actual_total_reps": session.get("completed_reps", 0),
        "completion_pct":    session.get("completion_pct", 0),

        "calm_pct":     emotion_pcts.get("calm", 0),
        "pleasure_pct": emotion_pcts.get("pleasure", 0),
        "frus_pct":     emotion_pcts.get("frustration", 0),

        "plan_adjustments_summary": _adjustments_summary(session_dir),

        "br_bpm_mean": _br_stat(session_dir, "mean"),
        "br_bpm_peak": _br_stat(session_dir, "peak"),

        "user_notes":  _read_notes(session_dir),
    }


def _baseline_quality(baseline_min) -> str:
    try:
        bm = float(baseline_min)
    except (TypeError, ValueError):
        bm = 4
    if bm >= 4:    return "好 (90 windows 个性化基线)"
    if bm >= 3:    return "可用 (60 windows 个性化基线)"
    if bm >= 2:    return "略弱 (30 windows 个性化基线)"
    if bm >= 1:    return "差 (无个性化数据,退化到全局基线)"
    return "跳过基线 (开发/演示模式)"


def _emotion_distribution(session_dir: Path) -> dict[str, int]:
    f = session_dir / "emotion_timeline.jsonl"
    if not f.exists():
        return {}
    counts: dict[str, int] = {}
    total = 0
    try:
        for line in f.open("r", encoding="utf-8"):
            try:
                d = json.loads(line)
                lbl = d.get("label")
                if lbl:
                    counts[lbl] = counts.get(lbl, 0) + 1
                    total += 1
            except Exception:
                continue
    except Exception:
        return {}
    if total == 0:
        return {}
    return {k: round(v * 100 / total) for k, v in counts.items()}


def _adjustments_summary(session_dir: Path) -> str:
    f = session_dir / "plan_adjustments.jsonl"
    if not f.exists():
        return "训练中系统未触发计划调整事件"
    triggers: list[str] = []
    try:
        for line in f.open("r", encoding="utf-8"):
            try:
                d = json.loads(line)
                trig = d.get("trigger")
                if trig:
                    triggers.append(trig)
            except Exception:
                continue
    except Exception:
        pass
    if not triggers:
        return "训练中系统未触发计划调整事件"
    return f"训练中系统检测到 {len(triggers)} 次事件: {', '.join(triggers)}"


def _br_stat(session_dir: Path, stat: str) -> float:
    f = session_dir / "emotion_timeline.jsonl"
    if not f.exists():
        return 0.0
    bpms: list[float] = []
    try:
        for line in f.open("r", encoding="utf-8"):
            try:
                d = json.loads(line)
                b = d.get("br_bpm")
                if b is not None:
                    bpms.append(float(b))
            except Exception:
                continue
    except Exception:
        return 0.0
    if not bpms:
        return 0.0
    if stat == "mean":
        return round(sum(bpms) / len(bpms), 1)
    if stat == "peak":
        return round(max(bpms), 1)
    return 0.0


def _read_notes(session_dir: Path) -> str:
    f = session_dir / "user_notes.txt"
    if not f.exists():
        return "(患者未填写备注)"
    try:
        text = f.read_text(encoding="utf-8").strip()
        return text if text else "(患者未填写备注)"
    except Exception:
        return "(患者未填写备注)"
