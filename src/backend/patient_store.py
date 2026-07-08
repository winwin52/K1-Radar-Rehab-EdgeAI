"""
PatientStore — file-based patient profile + session history persistence.

Directory layout:
    patients/
    └── <name>/
        ├── profile.json            patient metadata + default plan
        └── sessions/
            └── 20260627_193015/
                ├── session.json    finalized session record
                ├── emotion_timeline.jsonl     (Phase 5+)
                ├── plan_adjustments.jsonl     (Phase 5+)
                ├── user_notes.txt             (Phase 2+ optional)
                └── ai_assessment.md           (Phase 6+)

`name` is used directly as the directory name; we sanitize it to prevent
path traversal and OS-illegal characters, but otherwise keep human-readable
Chinese folders for offline operator inspection.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .plan import Plan
from .journey import DEFAULT_CYCLE_WEEKS, new_journey as _new_journey


# ---- Path helpers ---------------------------------------------------

_FORBIDDEN_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name: str) -> str:
    """Strip OS-illegal characters; collapse whitespace; raise on empty / suspicious."""
    s = _FORBIDDEN_NAME_CHARS.sub("", name).strip()
    if not s or s in {".", ".."}:
        raise ValueError(f"非法患者姓名: {name!r}")
    if s.startswith("."):
        # Reject leading dots: prevents hidden dirs and traversal-shaped names
        raise ValueError(f"姓名不能以 '.' 开头: {name!r}")
    if len(s) > 64:
        raise ValueError(f"姓名过长 (>64 字符): {name!r}")
    return s


# ---- Data classes ---------------------------------------------------

@dataclass
class PatientProfile:
    name: str
    injury_side: str = "left"               # "left" | "right" | "bilateral"
    injury_date: str | None = None
    stage_weeks_post_injury: int = 0
    doctor: str | None = None
    notes: str = ""
    default_plan: dict = field(default_factory=lambda: Plan.from_default().to_dict())
    # ── Journey / gamification (Phase 9 adherence layer) ─────────────────
    # The total rehab cycle this patient is committed to. Used by the
    # mountain-ascent metaphor on the HDMI screen to compute target
    # elevation. Set by the doctor / operator at creation; editable later.
    rehab_cycle_total_weeks: int = DEFAULT_CYCLE_WEEKS
    # ISO date when the first session ran; left blank until then.
    rehab_cycle_started_at: str = ""
    # Live journey state (mutated by SessionManager._finalize via journey.ledger).
    # Don't edit this by hand; use the ledger so milestones/titles stay consistent.
    journey: dict = field(default_factory=_new_journey)
    # ─────────────────────────────────────────────────────────────────────
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PatientProfile:
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in d.items() if k in known}
        # Backward compat: older profiles don't have journey/cycle fields.
        # Dataclass defaults handle the absence, but ensure dict-shaped fields
        # are at least the right shape if they exist but malformed.
        if "journey" in clean and not isinstance(clean["journey"], dict):
            clean["journey"] = _new_journey()
        return cls(**clean)


# ---- Exceptions ----------------------------------------------------

class PatientNotFoundError(Exception): ...
class PatientAlreadyExistsError(Exception): ...
class SessionNotFoundError(Exception): ...


# ---- Store ---------------------------------------------------------

class PatientStore:
    """File-backed CRUD for patient profiles + session histories."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Lazy-create a trash bin instead of hard-deleting (medical data)
        self._trash = self.root / ".trash"

    # ---- Profile CRUD ------------------------------------------------

    def list_patients(self) -> list[dict]:
        """Brief listing: name + last session timestamp + session count."""
        out = []
        for sub in sorted(self.root.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            profile_file = sub / "profile.json"
            if not profile_file.exists():
                continue
            try:
                with profile_file.open("r", encoding="utf-8") as f:
                    p = json.load(f)
            except Exception:
                continue
            sess = list((sub / "sessions").glob("*/session.json")) if (sub / "sessions").exists() else []
            last = max((s.stat().st_mtime for s in sess), default=None)
            out.append({
                "name": p.get("name", sub.name),
                "injury_side": p.get("injury_side"),
                "session_count": len(sess),
                "last_session_ts": last,
                "created_at": p.get("created_at"),
                # Phase 9: surface journey/title in the list view
                "rehab_cycle_total_weeks": p.get("rehab_cycle_total_weeks",
                                                 DEFAULT_CYCLE_WEEKS),
                "journey": p.get("journey", _new_journey()),
            })
        return out

    def get(self, name: str) -> PatientProfile:
        sname = sanitize_name(name)
        f = self.root / sname / "profile.json"
        if not f.exists():
            raise PatientNotFoundError(f"未找到患者: {name}")
        with f.open("r", encoding="utf-8") as fp:
            return PatientProfile.from_dict(json.load(fp))

    def exists(self, name: str) -> bool:
        try:
            sname = sanitize_name(name)
        except ValueError:
            return False
        return (self.root / sname / "profile.json").exists()

    def create(self, profile: PatientProfile) -> PatientProfile:
        sname = sanitize_name(profile.name)
        profile_dir = self.root / sname
        if profile_dir.exists():
            raise PatientAlreadyExistsError(f"患者已存在: {profile.name}")
        (profile_dir / "sessions").mkdir(parents=True)
        self._write_profile(sname, profile)
        return profile

    def update(self, name: str, patch: dict) -> PatientProfile:
        sname = sanitize_name(name)
        existing = self.get(name)
        merged = asdict(existing)
        # Only update known fields, never overwrite name (use rename instead)
        for k, v in patch.items():
            if k in {"name", "created_at"}:
                continue
            if k in merged:
                merged[k] = v
        merged["updated_at"] = datetime.now().isoformat(timespec="seconds")
        new_profile = PatientProfile.from_dict(merged)
        self._write_profile(sname, new_profile)
        return new_profile

    def delete(self, name: str) -> None:
        """Soft delete: move to .trash/<name>_<ts>/. Medical data is precious."""
        sname = sanitize_name(name)
        src = self.root / sname
        if not src.exists():
            raise PatientNotFoundError(f"未找到患者: {name}")
        self._trash.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.move(str(src), str(self._trash / f"{sname}_{ts}"))

    def _write_profile(self, sname: str, profile: PatientProfile) -> None:
        f = self.root / sname / "profile.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to tmp, then rename
        tmp = f.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(profile.to_dict(), fp, ensure_ascii=False, indent=2)
        tmp.replace(f)

    # ---- Session history ---------------------------------------------

    def session_dir(self, patient_name: str, session_id: str) -> Path:
        sname = sanitize_name(patient_name)
        return self.root / sname / "sessions" / session_id

    def create_session_dir(self, patient_name: str, session_id: str) -> Path:
        d = self.session_dir(patient_name, session_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_sessions(self, patient_name: str | None = None) -> list[dict]:
        """List session summaries (all patients or one)."""
        out: list[dict] = []
        if patient_name is not None:
            patients = [sanitize_name(patient_name)]
        else:
            patients = [p.name for p in self.root.iterdir()
                        if p.is_dir() and not p.name.startswith(".")]
        for pname in patients:
            sess_root = self.root / pname / "sessions"
            if not sess_root.exists():
                continue
            for sd in sorted(sess_root.iterdir(), reverse=True):
                if not sd.is_dir():
                    continue
                sf = sd / "session.json"
                if not sf.exists():
                    continue
                try:
                    with sf.open("r", encoding="utf-8") as fp:
                        sj = json.load(fp)
                except Exception:
                    continue
                out.append({
                    "patient": pname,
                    "session_id": sd.name,
                    "start": sj.get("start"),
                    "end": sj.get("end"),
                    "duration_s": sj.get("duration_s"),
                    "completion_pct": sj.get("completion_pct"),
                    "status": sj.get("status", "completed"),
                    "has_assessment": (sd / "ai_assessment.md").exists(),
                })
        return out

    def get_session(self, patient_name: str, session_id: str) -> dict:
        sf = self.session_dir(patient_name, session_id) / "session.json"
        if not sf.exists():
            raise SessionNotFoundError(f"Session 不存在: {patient_name}/{session_id}")
        with sf.open("r", encoding="utf-8") as fp:
            return json.load(fp)

    def write_session(self, patient_name: str, session_id: str, data: dict) -> None:
        """Atomic write of session.json."""
        sd = self.create_session_dir(patient_name, session_id)
        f = sd / "session.json"
        tmp = f.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        tmp.replace(f)

    def append_jsonl(self, patient_name: str, session_id: str,
                     filename: str, record: dict) -> None:
        """Append one record to <session>/<filename>.jsonl. Used for timeline streams."""
        sd = self.create_session_dir(patient_name, session_id)
        f = sd / filename
        with f.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
