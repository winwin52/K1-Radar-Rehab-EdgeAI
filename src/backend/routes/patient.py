"""Patient profile CRUD endpoints — /api/patients/*"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..journey import DEFAULT_CYCLE_WEEKS, summarize as journey_summarize
from ..patient_store import (PatientAlreadyExistsError, PatientNotFoundError,
                             PatientProfile, sanitize_name)
from ..plan import Plan
from ..session_manager import get_patient_store

router = APIRouter()


class PatientCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    injury_side: str = "left"
    injury_date: Optional[str] = None
    stage_weeks_post_injury: int = 0
    doctor: Optional[str] = None
    notes: str = ""
    # If omitted, falls back to clinical default
    default_plan: Optional[dict] = None
    # Phase 9: rehab cycle length in weeks (4–52 sensible range).
    # Default 12 weeks. Editable later.
    rehab_cycle_total_weeks: int = Field(DEFAULT_CYCLE_WEEKS, ge=1, le=52)


class PatientUpdateRequest(BaseModel):
    injury_side: Optional[str] = None
    injury_date: Optional[str] = None
    stage_weeks_post_injury: Optional[int] = None
    doctor: Optional[str] = None
    notes: Optional[str] = None
    default_plan: Optional[dict] = None
    # Phase 9: doctor can adjust the cycle (e.g. extend after a setback).
    # Lengthening keeps existing elevation, just lowers the percentage; the
    # patient automatically "loses" titles which is correct — they need to
    # earn them back at the new pace.
    rehab_cycle_total_weeks: Optional[int] = Field(None, ge=1, le=52)


@router.get("")
async def list_patients():
    return {"patients": get_patient_store().list_patients()}


@router.post("")
async def create_patient(req: PatientCreateRequest):
    try:
        sanitize_name(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    plan_dict = req.default_plan or Plan.from_default().to_dict()
    profile = PatientProfile(
        name=req.name,
        injury_side=req.injury_side,
        injury_date=req.injury_date,
        stage_weeks_post_injury=req.stage_weeks_post_injury,
        doctor=req.doctor,
        notes=req.notes,
        default_plan=plan_dict,
        rehab_cycle_total_weeks=req.rehab_cycle_total_weeks,
    )
    try:
        get_patient_store().create(profile)
    except PatientAlreadyExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "patient": profile.to_dict()}


@router.get("/{name}")
async def get_patient(name: str):
    try:
        return get_patient_store().get(name).to_dict()
    except PatientNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{name}")
async def update_patient(name: str, req: PatientUpdateRequest):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        profile = get_patient_store().update(name, patch)
    except PatientNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "patient": profile.to_dict()}


@router.delete("/{name}")
async def delete_patient(name: str):
    try:
        get_patient_store().delete(name)
    except PatientNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "msg": "已移到回收站"}


@router.get("/{name}/sessions")
async def list_patient_sessions(name: str):
    if not get_patient_store().exists(name):
        raise HTTPException(status_code=404, detail=f"未找到患者: {name}")
    return {"sessions": get_patient_store().list_sessions(name)}


# ---- Journey endpoints (Phase 9) ------------------------------------

@router.get("/{name}/journey")
async def get_patient_journey(name: str):
    """
    Return the patient's Journey snapshot ready for UI consumption.
    Includes computed fields (progress_pct, title, stage, next_milestone).
    """
    try:
        prof = get_patient_store().get(name)
    except PatientNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    summary = journey_summarize(prof.journey, prof.rehab_cycle_total_weeks)
    summary["cycle_weeks"] = prof.rehab_cycle_total_weeks
    summary["cycle_started_at"] = prof.rehab_cycle_started_at
    summary["raw"] = prof.journey      # for debugging / future custom views
    return summary
