"""
Backend service for the rehabilitation system.

Process B of the K1 architecture:
  - FastAPI + uvicorn (HTTP REST + WebSocket on port 8000)
  - Coordinator: bridges Process A (sensing) ↔ B (this) ↔ C (screen)
  - Session FSM, Coach decision, Audio engine, AI worker

See `README.md` for module map.
"""
