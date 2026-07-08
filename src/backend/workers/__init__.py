"""
Async background workers.

Workers run as asyncio tasks within the FastAPI process,
consuming from in-process queues without blocking the main event loop.
"""
