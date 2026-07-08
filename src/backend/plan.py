"""
Plan — rehabilitation training parameters.

A Plan is a snapshot of training parameters that drives the SessionFSM.
It can come from:
  - the global clinical default (config/default_plan.json)
  - a per-patient override (patient profile's default_plan)
  - a session-specific override (caller passes plan_override on /api/session/start)

Plans are immutable dataclasses; modifications produce new instances.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from typing import Any

# Path resolution: plan.py is in backend/, config/ is at scripts/realtime3.0/
_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN_FILE = _ROOT / "config" / "default_plan.json"


@dataclass(frozen=True)
class Plan:
    # Training volume
    sets: int = 3
    reps_per_set: int = 12
    # Per-rep timing (seconds, float for sub-second precision)
    lift_s: float = 2.0
    hold_s: float = 3.0
    lower_s: float = 2.0
    rest_between_rep_s: float = 2.0
    # Inter-set rest (whole seconds is fine)
    rest_between_set_s: int = 30
    # Target ROM (informational; rehab device doesn't enforce)
    target_angle_deg: int = 30
    # Baseline duration before training
    baseline_min: int = 4
    # Coach adjustment rules — kept as raw dict for now (Coach reads it)
    adjustment_rules: dict[str, Any] = field(default_factory=dict)

    # ---- Constructors --------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> Plan:
        """Build a Plan from a (possibly partial) dict, ignoring unknown keys."""
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        # Coerce types defensively (JSON numbers can come as int when float expected)
        for k in ("lift_s", "hold_s", "lower_s", "rest_between_rep_s"):
            if k in kwargs:
                kwargs[k] = float(kwargs[k])
        for k in ("sets", "reps_per_set", "rest_between_set_s",
                  "target_angle_deg", "baseline_min"):
            if k in kwargs:
                kwargs[k] = int(kwargs[k])
        return cls(**kwargs)

    @classmethod
    def from_default(cls) -> Plan:
        """Load the clinical default from config/default_plan.json."""
        if not DEFAULT_PLAN_FILE.exists():
            return cls()  # built-in defaults
        with DEFAULT_PLAN_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Strip _comment / _source meta keys
        clean = {k: v for k, v in data.items() if not k.startswith("_")}
        return cls.from_dict(clean)

    # ---- Conversions ---------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    def with_overrides(self, overrides: dict[str, Any] | None) -> Plan:
        """Return a new Plan with the given fields overridden."""
        if not overrides:
            return self
        known = {f for f in self.__dataclass_fields__}
        clean = {k: v for k, v in overrides.items() if k in known and k != "adjustment_rules"}
        # Type coercion (same as from_dict)
        for k in ("lift_s", "hold_s", "lower_s", "rest_between_rep_s"):
            if k in clean:
                clean[k] = float(clean[k])
        for k in ("sets", "reps_per_set", "rest_between_set_s",
                  "target_angle_deg", "baseline_min"):
            if k in clean:
                clean[k] = int(clean[k])
        return replace(self, **clean)

    # ---- Derived quantities -------------------------------------------

    def rep_cycle_s(self) -> float:
        """Wall-clock seconds for one complete rep (lift+hold+lower+rest)."""
        return self.lift_s + self.hold_s + self.lower_s + self.rest_between_rep_s

    def set_duration_s(self) -> float:
        """Seconds for one full set (excluding inter-set rest)."""
        return self.rep_cycle_s() * self.reps_per_set

    def training_duration_s(self) -> float:
        """Seconds for all training (excluding baseline)."""
        return (self.set_duration_s() * self.sets
                + self.rest_between_set_s * (self.sets - 1))

    def total_session_s(self) -> float:
        """Estimated total session wall-clock seconds (baseline + training)."""
        return self.baseline_min * 60.0 + self.training_duration_s()

    # ---- Validation ---------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty = OK).

        Lower bounds intentionally permissive (0.1s lift, 0 baseline) to
        enable dev/demo/test runs. Clinical-quality bounds are enforced by
        the medical team via the per-patient profile, not here.
        """
        errs: list[str] = []
        if self.sets < 1 or self.sets > 10:
            errs.append(f"组数应在 1-10 之间 (当前 {self.sets})")
        if self.reps_per_set < 1 or self.reps_per_set > 50:
            errs.append(f"单组次数应在 1-50 之间 (当前 {self.reps_per_set})")
        if not (0.1 <= self.lift_s <= 10):
            errs.append(f"上抬时长应在 0.1-10s 之间 (当前 {self.lift_s})")
        if not (0.1 <= self.hold_s <= 30):
            errs.append(f"保持时长应在 0.1-30s 之间 (当前 {self.hold_s})")
        if not (0.1 <= self.lower_s <= 10):
            errs.append(f"下降时长应在 0.1-10s 之间 (当前 {self.lower_s})")
        if not (0 <= self.rest_between_rep_s <= 30):
            errs.append(f"次间间歇应在 0-30s 之间 (当前 {self.rest_between_rep_s})")
        if not (0 <= self.rest_between_set_s <= 300):
            errs.append(f"组间休息应在 0-300s 之间 (当前 {self.rest_between_set_s})")
        if not (0 <= self.baseline_min <= 10):
            errs.append(f"基线时长应在 0-10 min 之间 (0 表示跳过, 当前 {self.baseline_min})")
        return errs

    def baseline_quality_warning(self) -> str | None:
        """Return a Chinese warning if baseline_min is too short to
        produce a personal baseline. None if acceptable."""
        # rehab_engine 内部:
        #   前 60s = buffer-fill (无特征提取)
        #   60s ~ baseline_min*60s = 个性化基线特征采集 (每 2s 一个 window)
        # baseline_min=1: 0 个 windows → 退化到 global_calm_mean
        # baseline_min=2: 30 windows
        # baseline_min=3: 60 windows
        # baseline_min=4: 90 windows (推荐)
        if self.baseline_min < 1:
            return None     # 0 = 显式跳过,不警告
        if self.baseline_min == 1:
            return ("基线仅 1 分钟,全部用于 buffer 填充,无个性化数据 → "
                    "退化到全局基线 (识别准确率会降几个点)")
        if self.baseline_min == 2:
            return "基线 2 分钟仅有 30 个个性化窗口 (推荐 90),准确率略受影响"
        if self.baseline_min == 3:
            return "基线 3 分钟有 60 个个性化窗口 (推荐 90),可用"
        return None         # >= 4 推荐
