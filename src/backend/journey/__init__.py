"""Journey — gamification layer (mountain-climbing metaphor)."""
from .progression import (
    ELEVATION_PER_REP,
    TARGET_PER_WEEK,
    DEFAULT_CYCLE_WEEKS,
    MILESTONES_M,
    TITLE_CUTOFFS,
    STAGES,
    target_elevation,
    elevation_for_reps,
    progress_pct,
    title_for_progress,
    stage_for_progress,
    stage_index,
    newly_unlocked_milestones,
    title_upgraded,
    streak_after_session,
    summarize,
)
from .ledger import (
    new_journey,
    apply_session,
    LedgerUpdate,
)

__all__ = [
    "ELEVATION_PER_REP", "TARGET_PER_WEEK", "DEFAULT_CYCLE_WEEKS",
    "MILESTONES_M", "TITLE_CUTOFFS", "STAGES",
    "target_elevation", "elevation_for_reps", "progress_pct",
    "title_for_progress", "stage_for_progress", "stage_index",
    "newly_unlocked_milestones", "title_upgraded",
    "streak_after_session", "summarize",
    "new_journey", "apply_session", "LedgerUpdate",
]
