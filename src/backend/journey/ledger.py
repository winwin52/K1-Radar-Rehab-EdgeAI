"""
Ledger — applies a finished session to a patient's Journey state.

Called from SessionManager._finalize after the session.json is written.
Pure function: takes (old_journey, completed_reps, cycle_weeks, today) →
returns (new_journey, list_of_celebration_events).

This is the single place where journey state mutates. Tests live next to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import progression as prog


@dataclass
class LedgerUpdate:
    """Result of applying a session to the patient's journey.

    Consumers:
      - SessionManager: persists new_journey to the patient profile
      - Screen: renders the celebration events that just triggered
      - WebSocket/ZMQ: broadcasts these so the HDMI scene knows what to play
    """
    new_journey: dict
    elevation_gained: int
    new_milestones: list[int] = field(default_factory=list)
    title_change: tuple[str, str] | None = None     # (old, new) if upgraded
    streak_changed: bool = False
    is_new_journey: bool = False                    # first ever session?

    def has_celebration(self) -> bool:
        """True if anything notable happened that the screen should celebrate."""
        return bool(self.new_milestones or self.title_change)

    def to_event_payload(self) -> dict:
        """Compact payload for broadcasting to the screen via device_state."""
        return {
            "elevation_gained":  self.elevation_gained,
            "new_milestones":    list(self.new_milestones),
            "title_change":      list(self.title_change) if self.title_change else None,
            "streak_changed":    self.streak_changed,
            "is_new_journey":    self.is_new_journey,
        }


def new_journey() -> dict:
    """A fresh journey dict, used when creating a new patient profile."""
    return {
        "total_elevation_m":    0,
        "sessions_completed":   0,
        "last_session_date":    "",
        "streak_days":          0,
        "unlocked_milestones":  [],
        "current_title":        prog.title_for_progress(0.0),
    }


def apply_session(old_journey: dict | None,
                  completed_reps: int,
                  cycle_weeks: int,
                  today: str | None = None) -> LedgerUpdate:
    """
    Apply a finished session to the journey state.

    Parameters
    ----------
    old_journey   : the patient's journey before this session (or None)
    completed_reps: number of reps actually completed in the session
                    (we trust SessionResult.completed_reps; aborted-but-some-
                    done sessions still count their done reps).
    cycle_weeks   : patient.rehab_cycle_total_weeks
    today         : ISO 'YYYY-MM-DD' (default: real today). Test hook.

    Returns
    -------
    LedgerUpdate with:
      - new_journey to persist
      - events the screen should celebrate
    """
    today = today or date.today().isoformat()
    is_new = old_journey is None or not old_journey.get("last_session_date")
    j = dict(old_journey) if old_journey else new_journey()

    # ---- Compute delta ------------------------------------------------
    target_m = prog.target_elevation(cycle_weeks)
    prev_elev = int(j.get("total_elevation_m", 0))
    elev_gained = prog.elevation_for_reps(completed_reps)
    curr_elev = prev_elev + elev_gained

    # ---- Milestone checks --------------------------------------------
    already_unlocked = set(j.get("unlocked_milestones", []))
    newly_hit = [m for m in prog.newly_unlocked_milestones(prev_elev, curr_elev)
                 if m not in already_unlocked]

    # ---- Title check -------------------------------------------------
    prev_pct = prog.progress_pct(prev_elev, target_m)
    curr_pct = prog.progress_pct(curr_elev, target_m)
    title_change = prog.title_upgraded(prev_pct, curr_pct)

    # ---- Streak update -----------------------------------------------
    prev_streak = int(j.get("streak_days", 0))
    last_date = j.get("last_session_date", "")
    new_streak = prog.streak_after_session(last_date, today, prev_streak)
    streak_changed = new_streak != prev_streak

    # ---- Build new journey -------------------------------------------
    j["total_elevation_m"] = curr_elev
    j["sessions_completed"] = int(j.get("sessions_completed", 0)) + (1 if completed_reps > 0 else 0)
    j["last_session_date"] = today
    j["streak_days"] = new_streak
    j["unlocked_milestones"] = sorted(already_unlocked | set(newly_hit))
    j["current_title"] = prog.title_for_progress(curr_pct)

    return LedgerUpdate(
        new_journey=j,
        elevation_gained=elev_gained,
        new_milestones=newly_hit,
        title_change=title_change,
        streak_changed=streak_changed,
        is_new_journey=is_new,
    )
