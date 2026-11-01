"""Process-wide admission for model calls (LDR-0013).

V1 kept a growing per-script delay after each 429. Here one gate per worker
process caps concurrent calls, optionally paces them, and turns a 429 into a
cooldown every caller waits out, so a rate-limited gateway sees the whole
process back off at once instead of each case retrying on its own schedule.

Used from a single event loop (``ModelLoop``), so no locks are needed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class RateGate:
    def __init__(
        self,
        *,
        max_concurrency: int,
        requests_per_minute: float | None = None,
        cooldown_sec: float = 5.0,
        cooldown_max_sec: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._slots = asyncio.Semaphore(max_concurrency)
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._cooldown_sec = cooldown_sec
        self._cooldown_max_sec = cooldown_max_sec
        self._clock = clock
        self._sleep = sleep
        self._next_start = 0.0
        self._cooldown_until = 0.0
        self._strikes = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one call slot: after any cooldown, under the concurrency cap, at the paced rate."""
        await self._wait_cooldown()
        async with self._slots:
            await self._wait_cooldown()  # a 429 may have landed while this call queued for a slot
            await self._pace()
            yield

    def rate_limited(self, retry_after_sec: float | None = None) -> float:
        """Record a 429 and return the cooldown it imposes on every caller.

        429s from calls already in flight during a cooldown are the same episode:
        they may lengthen it to their Retry-After but don't escalate it.
        """
        now = self._clock()
        if now >= self._cooldown_until:
            self._strikes += 1
        delay = min(self._cooldown_max_sec, self._cooldown_sec * 2 ** (self._strikes - 1))
        if retry_after_sec:
            delay = max(delay, retry_after_sec)
        self._cooldown_until = max(self._cooldown_until, now + delay)
        return self._cooldown_until - now

    def succeeded(self) -> None:
        self._strikes = 0

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self._clock())

    async def _wait_cooldown(self) -> None:
        while (wait := self._cooldown_until - self._clock()) > 0:
            await self._sleep(wait)

    async def _pace(self) -> None:
        if not self._interval:
            return
        now = self._clock()
        start = max(now, self._next_start)
        self._next_start = start + self._interval
        if start > now:
            await self._sleep(start - now)
