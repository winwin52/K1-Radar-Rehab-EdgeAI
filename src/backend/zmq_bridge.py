"""
ZMQ PUB bridge from backend (Process B) to screen (Process C).

Topic protocol:
    b"state"  →  JSON device status payload

The screen process subscribes via plain (sync) zmq in a background thread;
backend publishes via zmq.asyncio so it integrates with the FastAPI event loop.
"""

import json
import zmq
import zmq.asyncio


DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"


class ZMQStatePublisher:
    """Async publisher that broadcasts device status JSON to the screen process."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT):
        self.endpoint = endpoint
        self._ctx: zmq.asyncio.Context | None = None
        self._sock: zmq.asyncio.Socket | None = None

    def start(self) -> None:
        # zmq.asyncio.Context.instance() returns a shared context. Using a
        # private one keeps test/teardown semantics clean.
        self._ctx = zmq.asyncio.Context()
        self._sock = self._ctx.socket(zmq.PUB)
        # SNDHWM small: state updates are tiny and we don't want backlog
        self._sock.setsockopt(zmq.SNDHWM, 100)
        self._sock.bind(self.endpoint)
        print(f"[ZMQ] PUB bound to {self.endpoint}")

    async def publish(self, payload: dict) -> None:
        if self._sock is None:
            return
        try:
            await self._sock.send_multipart([
                b"state",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ])
        except Exception as e:
            print(f"[ZMQ] publish error: {e!r}")

    def stop(self) -> None:
        if self._sock is not None:
            self._sock.close(linger=0)
            self._sock = None
        if self._ctx is not None:
            self._ctx.term()
            self._ctx = None
