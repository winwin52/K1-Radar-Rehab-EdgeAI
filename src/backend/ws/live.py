"""
/ws/live — WebSocket push channel to web clients.

On connect:
    - server immediately sends current device status
On state change:
    - server broadcasts new status to all connected clients
Heartbeat:
    - if no client message within 30s, server sends {"heartbeat": true}
    - client may send "ping" → server replies "pong"

The connection pool is wired to DeviceStateManager subscriptions in
server.py's lifespan (not here, to keep lifecycle explicit).
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..device_state import get_manager

log = logging.getLogger(__name__)
router = APIRouter()


class ConnectionPool:
    """Owns the set of live WebSocket connections + broadcast helper."""

    def __init__(self):
        self._connections: set[WebSocket] = set()

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        if not self._connections:
            return
        msg = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    def size(self) -> int:
        return len(self._connections)


pool = ConnectionPool()


@router.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    await pool.add(ws)
    try:
        # Send initial state immediately so the UI doesn't sit blank.
        await ws.send_text(
            json.dumps(get_manager().get_status(), ensure_ascii=False)
        )
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                if msg == "ping":
                    await ws.send_text("pong")
                # Any other client→server messages are ignored in Phase 1
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"heartbeat": True}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("[/ws/live] error: %r", e)
    finally:
        pool.remove(ws)
