"""One asyncio event loop per process for model calls (LDR-0013).

Workers run cases on threads and stage handlers are plain functions (P6), but
model calls are async, as in V1: each handler hands its coroutine to this loop
and blocks until it finishes. Every case in the process then shares one
``GatewayModelClient`` and one ``RateGate``, so the concurrency cap and the
429 cooldown apply to the whole process, and a handler can still run its own
calls concurrently (``asyncio.gather``) inside the coroutine it submits.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from cfr_v2.telemetry import attach, current_context, detach

T = TypeVar("T")


class ModelLoop:
    def __init__(self, name: str = "cfr-model-loop") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name=name, daemon=True)
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the loop from any other thread and return its result (or raise its error).

        The caller's trace context goes with it, so model-call spans nest under the stage span.
        """
        if threading.current_thread() is self._thread:
            raise RuntimeError("ModelLoop.run called from the loop itself; await the coroutine instead")
        future = asyncio.run_coroutine_threadsafe(_in_context(current_context(), coro), self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def close(self, timeout: float = 5.0) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self._loop.close()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop


async def _in_context[T](ctx: Any, coro: Coroutine[Any, Any, T]) -> T:
    token = attach(ctx)
    try:
        return await coro
    finally:
        detach(token)


_default: ModelLoop | None = None
_default_lock = threading.Lock()


def model_loop() -> ModelLoop:
    """The process's model loop, started on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = ModelLoop()
        return _default


def close_model_loop() -> None:
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
            _default = None
