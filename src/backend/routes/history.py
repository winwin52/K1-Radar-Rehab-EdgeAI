"""History endpoints — /api/history/*"""

import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..patient_store import SessionNotFoundError
from ..session_manager import get_patient_store
from ..workers.ai_worker import enqueue_assessment

router = APIRouter()


@router.get("")
async def list_history(patient: str | None = Query(default=None)):
    """All sessions for all patients, or filtered by patient name."""
    return {"sessions": get_patient_store().list_sessions(patient)}


@router.get("/{patient}/{session_id}")
async def get_session(patient: str, session_id: str):
    try:
        sj = get_patient_store().get_session(patient, session_id)
    except SessionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    sd = get_patient_store().session_dir(patient, session_id)
    sj["_artifacts"] = sorted(p.name for p in sd.iterdir() if p.is_file())
    return sj


@router.get("/{patient}/{session_id}/assessment")
async def get_assessment(patient: str, session_id: str):
    md = get_patient_store().session_dir(patient, session_id) / "ai_assessment.md"
    if not md.exists():
        raise HTTPException(status_code=404, detail="尚未生成评估报告")
    return {"content": md.read_text(encoding="utf-8")}


@router.post("/{patient}/{session_id}/regenerate")
async def regenerate_assessment(patient: str, session_id: str):
    """Manually re-trigger AI assessment generation for this session."""
    try:
        session_data = get_patient_store().get_session(patient, session_id)
    except SessionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    session_dir = get_patient_store().session_dir(patient, session_id)
    ok = enqueue_assessment(session_dir, session_data)
    if not ok:
        raise HTTPException(status_code=503, detail="AI 任务队列暂时不可用")
    return {"ok": True, "msg": "已加入生成队列,稍后查看"}


@router.get("/{patient}/{session_id}/download")
async def download_session(patient: str, session_id: str):
    """Return a streaming ZIP of the entire session directory."""
    sd = get_patient_store().session_dir(patient, session_id)
    if not sd.exists() or not (sd / "session.json").exists():
        raise HTTPException(status_code=404, detail="Session 不存在")

    def gen() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sd.iterdir():
                if f.is_file():
                    zf.write(f, arcname=f"{patient}/{session_id}/{f.name}")
        return buf.getvalue()

    data = gen()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{patient}_{session_id}.zip"',
            "Content-Length": str(len(data)),
        },
    )
