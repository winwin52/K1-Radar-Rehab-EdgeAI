"""Plan template endpoints — /api/plans/*"""

from fastapi import APIRouter

from ..plan import Plan

router = APIRouter()


@router.get("/default")
async def get_default_plan():
    """Read-only clinical default (from config/default_plan.json)."""
    plan = Plan.from_default()
    return {
        "plan": plan.to_dict(),
        "estimated_session_s": plan.total_session_s(),
        "rep_cycle_s": plan.rep_cycle_s(),
    }


@router.post("/validate")
async def validate_plan(plan_dict: dict):
    """Validate a plan dict; returns list of errors (empty if OK) + soft warnings."""
    try:
        plan = Plan.from_dict(plan_dict)
    except Exception as e:
        return {"ok": False, "errors": [f"无法解析计划: {e}"], "warnings": []}
    errs = plan.validate()
    warnings = []
    bw = plan.baseline_quality_warning()
    if bw:
        warnings.append(bw)
    return {
        "ok": len(errs) == 0,
        "errors": errs,
        "warnings": warnings,
        "estimated_session_s": plan.total_session_s(),
    }
