"""
FastAPI backend entrypoint (Process B).

Run development:
    cd scripts/realtime3.0
    python -m backend.server
    # or
    uvicorn backend.server:app --reload --host 0.0.0.0 --port 8000

Run production (K1 / systemd):
    uvicorn backend.server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import multiprocessing as mp
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .audio_engine import init_engine as init_audio_engine
from .coordinator import get_coordinator
from .device_state import get_manager
from .routes import device, session, patient, plan, history
from .session_manager import init_session_manager
from .workers import ai_worker
from .ws import live
from .ws.live import pool as ws_pool
from .zmq_bridge import ZMQStatePublisher


# ---- Paths -----------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent   # scripts/realtime3.0/
WEBAPP_DIR   = ROOT / "webapp"
PATIENTS_DIR = ROOT / "patients"
ASSETS_DIR   = ROOT / "assets"


# ---- Logging ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("backend")


# ---- App lifespan ---------------------------------------------------

zmq_pub = ZMQStatePublisher()

# Heartbeat interval (seconds) — re-publishes current state regardless of
# whether anything changed. Solves the ZMQ "slow joiner" problem: when the
# screen process subscribes late or restarts mid-session, it picks up the
# current state within this interval, instead of waiting for the next
# real state change.
ZMQ_HEARTBEAT_S = 1.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting backend...")
    zmq_pub.start()

    # ─── Sensing queue (shared, lives for the whole backend process) ────
    # Created once, passed to BOTH the Coordinator (consumer) and the
    # SessionManager (which gives it to each sensing child process).
    sensing_q = mp.Queue(maxsize=200)
    log.info("Created sensing mp.Queue (maxsize=200)")

    # ─── SessionManager (Phase 2) — now receives the sensing queue too ─
    PATIENTS_DIR.mkdir(parents=True, exist_ok=True)
    init_session_manager(PATIENTS_DIR, sensing_queue=sensing_q)
    log.info("SessionManager ready (patients_dir=%s)", PATIENTS_DIR)

    # ─── Coordinator (Phase 5) — consumes sensing events ───────────────
    coordinator = get_coordinator()
    coord_stop = asyncio.Event()
    coord_task = asyncio.create_task(
        coordinator.run(sensing_q, coord_stop), name="coordinator")

    # ─── AI Worker (Phase 6) — async LLM tasks (assessment, encourage) ──
    ai_stop = asyncio.Event()
    ai_task = asyncio.create_task(ai_worker.run_worker(ai_stop), name="ai-worker")
    log.info("AI worker task scheduled")

    # ─── Audio engine (Phase 7) — sounddevice persistent stream ─────────
    audio_engine = init_audio_engine(ASSETS_DIR)
    audio_started = audio_engine.start()
    if audio_started:
        log.info("Audio engine started (assets=%s)", ASSETS_DIR)
    else:
        log.warning("Audio engine NOT available — sounddevice missing or device error")

    mgr = get_manager()

    # Subscriber 1: broadcast state changes to screen process via ZMQ
    async def _on_change_zmq(status):
        await zmq_pub.publish(status.as_dict())

    # Subscriber 2: broadcast state changes to web clients via WebSocket
    async def _on_change_ws(status):
        await ws_pool.broadcast(status.as_dict())

    mgr.subscribe(_on_change_zmq)
    mgr.subscribe(_on_change_ws)

    # Push initial state through ZMQ so screen renders correctly on startup
    await zmq_pub.publish(mgr.get_status())

    # ZMQ heartbeat task — runs forever, re-publishes current state every
    # ZMQ_HEARTBEAT_S seconds. Cheap (a few hundred bytes); guarantees the
    # screen process catches up after a late start / restart.
    async def _heartbeat():
        while True:
            await asyncio.sleep(ZMQ_HEARTBEAT_S)
            try:
                await zmq_pub.publish(mgr.get_status())
            except Exception as e:
                log.warning("[ZMQ heartbeat] %r", e)

    hb_task = asyncio.create_task(_heartbeat(), name="zmq-heartbeat")

    log.info("Backend ready — state=IDLE")
    yield

    log.info("Shutting down backend...")
    coord_stop.set()
    ai_stop.set()
    hb_task.cancel()
    for t in (hb_task, coord_task, ai_task):
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    if audio_engine:
        audio_engine.stop()
    zmq_pub.stop()


# ---- App -------------------------------------------------------------

app = FastAPI(
    title="Rehab K1 Backend",
    version="0.1.0-phase1",
    description="Embedded rehab system controller (Process B)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # local LAN only; tighten in prod if exposed
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Routes ----------------------------------------------------------

app.include_router(device.router,  prefix="/api/device",   tags=["device"])
app.include_router(session.router, prefix="/api/session",  tags=["session"])
app.include_router(patient.router, prefix="/api/patients", tags=["patients"])
app.include_router(plan.router,    prefix="/api/plans",    tags=["plans"])
app.include_router(history.router, prefix="/api/history",  tags=["history"])
app.include_router(live.router,                            tags=["websocket"])


# ---- Static webapp serve --------------------------------------------

if WEBAPP_DIR.exists():
    # /static/* serves files inside webapp/
    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        idx = WEBAPP_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"message": "webapp/index.html not found"})
else:
    @app.get("/", include_in_schema=False)
    async def index():
        return JSONResponse({"message": "webapp/ directory missing"})


# ---- Entry for `python -m backend.server` ---------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,           # reload uses subprocess; lifespan/ZMQ get messy
        log_level="info",
    )
