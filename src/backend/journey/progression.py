"""
Journey — gamification layer for rehab adherence.

Single source of truth for the "mountain ascent" mechanic:
  - one rep ≈ ELEVATION_PER_REP meters
  - one rehab cycle (N weeks, configurable per patient) ≈ N × TARGET_PER_WEEK meters
  - five titles map to progress percentage (decouples from cycle length so
    a patient with an 8-week cycle and a patient with a 24-week cycle both
    feel the same progression pace)

This module is *pure*: no I/O, no logging. The backend's `ledger.py` is
what persists Journey state into patient profiles.
"""

from __future__ import annotations

# ---- Game mechanics (do not change without updating designs) -----------

ELEVATION_PER_REP = 5            # meters of "elevation" per completed rep
TARGET_PER_WEEK   = 500          # meters of target elevation per rehab week
DEFAULT_CYCLE_WEEKS = 12         # default rehab cycle length (medical default)

# Milestones (meters) that trigger a celebration animation on the screen.
# Spaced so an active patient hits one every ~4 sessions early, then less
# often as they approach the summit (where the journey becomes more
# psychologically meaningful and the rewards more rare).
MILESTONES_M: tuple[int, ...] = (100, 500, 1000, 2000, 3000, 4500, 6000, 8000, 10000)

# Titles by progress percentage of the patient's target.
# Cutoffs chosen so:
#   - 0-8%   beginner zone (most users start here, want to leave it fast)
#   - 8-25%  early commitment (~1/4 done, building habit)
#   - 25-50% mid-cycle (showing up consistently)
#   - 50-83% nearing peak (the hard last third)
#   - 83-100% summit guardian (the rest of the cycle and beyond)
TITLE_CUTOFFS: list[tuple[float, str]] = [
    (0.08, "新手徒步者"),
    (0.25, "山林漫步者"),
    (0.50, "登山者"),
    (0.83, "雪线征服者"),
    (1.01, "山顶守护者"),   # 1.01 so >100% still resolves
]

# Stage names (the visible "where you are" on the mountain). 6 stages so
# the mountain looks meaningful even for short cycles.
STAGES: tuple[str, ...] = ("山脚", "丛林", "营地", "雪线", "山脊", "山顶")


# ---- Pure functions ----------------------------------------------------

def target_elevation(weeks: int) -> int:
    """Total target elevation (meters) for a cycle of given weeks."""
    if weeks <= 0:
        weeks = DEFAULT_CYCLE_WEEKS
    return weeks * TARGET_PER_WEEK


def elevation_for_reps(completed_reps: int) -> int:
    """Meters earned from completing N reps."""
    if completed_reps <= 0:
        return 0
    return completed_reps * ELEVATION_PER_REP


def progress_pct(elevation_m: int, target_m: int) -> float:
    """Fraction (0.0–1.0+). >1.0 means the patient surpassed the cycle goal."""
    if target_m <= 0:
        return 0.0
    return max(0.0, elevation_m / target_m)


def title_for_progress(pct: float) -> str:
    """Map progress fraction to a title."""
    for cutoff, name in TITLE_CUTOFFS:
        if pct < cutoff:
            return name
    return TITLE_CUTOFFS[-1][1]


def stage_for_progress(pct: float) -> str:
    """Map progress fraction (0–1) to a stage name on the mountain."""
    idx = min(int(pct * len(STAGES)), len(STAGES) - 1)
    return STAGES[max(0, idx)]


def stage_index(pct: float) -> int:
    """0-based index into STAGES — useful for path rendering."""
    return max(0, min(int(pct * len(STAGES)), len(STAGES) - 1))


def newly_unlocked_milestones(prev_m: int, curr_m: int) -> list[int]:
    """Return milestones whose threshold lies in (prev_m, curr_m]."""
    if curr_m <= prev_m:
        return []
    return [m for m in MILESTONES_M if prev_m < m <= curr_m]


def title_upgraded(prev_pct: float, curr_pct: float) -> tuple[str, str] | None:
    """If the title changed across this delta, return (old, new). Else None."""
    a, b = title_for_progress(prev_pct), title_for_progress(curr_pct)
    return (a, b) if a != b else None


# ---- Streak math -------------------------------------------------------

def streak_after_session(last_session_date: str,
                          this_session_date: str,
                          prev_streak: int) -> int:
    """
    Update the consecutive-day streak.

    Dates are ISO 'YYYY-MM-DD' strings.

    Rules:
      - First-ever session: streak = 1
      - Same day repeat: streak unchanged (no double-counting)
      - Next calendar day: streak + 1
      - Otherwise (gap ≥ 2 days): streak resets to 1
    """
    if not last_session_date:
        return 1
    if this_session_date == last_session_date:
        return max(prev_streak, 1)
    # Calendar-day delta
    from datetime import date
    try:
        d1 = date.fromisoformat(last_session_date)
        d2 = date.fromisoformat(this_session_date)
    except Exception:
        return 1
    delta = (d2 - d1).days
    if delta == 1:
        return prev_streak + 1
    return 1            # gap or backwards (shouldn't happen) → reset


# ---- Compact summary for UI --------------------------------------------

def summarize(journey: dict, cycle_weeks: int) -> dict:
    """
    Build a UI-friendly summary from raw journey state.

    Used by both the HDMI screen and the webapp; never returns None fields.
    """
    elev = int(journey.get("total_elevation_m", 0))
    target = target_elevation(cycle_weeks)
    pct = progress_pct(elev, target)
    return {
        "elevation_m":      elev,
        "target_m":         target,
        "progress_pct":     round(pct, 4),
        "title":            title_for_progress(pct),
        "stage":            stage_for_progress(pct),
        "stage_index":      stage_index(pct),
        "sessions_completed": int(journey.get("sessions_completed", 0)),
        "streak_days":      int(journey.get("streak_days", 0)),
        "unlocked":         list(journey.get("unlocked_milestones", [])),
        "next_milestone":   next((m for m in MILESTONES_M if m > elev), None),
    }
