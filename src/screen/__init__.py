"""
Process C — HDMI touchscreen renderer.

pygame-based, subscribes to ZMQ PUB from backend for state updates,
sends touch events back via ZMQ REQ.
"""
