"""Process-wide admission for model calls (LDR-0013, LDR-0032).

V1 kept a growing per-script delay after each 429. Here one gate per worker
process caps concurrent calls, optionally paces them, and turns a 429 into a
cooldown every caller waits out, so a rate-limited gateway sees the whole
process back off at once instead of each case retrying on its own schedule.

The cap adapts, as TCP's congestion window does (LDR-0032). A worker starting on a full queue
used to send its whole cap of calls at once, and the gateway answered with a burst of 429s;
after each cooldown every waiting call resumed together, and the burst repeated. Now:

- **Slow start.** The gate allows ``start_concurrency`` calls at once, and doubles that after
  each round of successes (as many successes as calls allowed), up to the cap.
- **Back off.** The first 429 of an episode halves the allowance; from there it grows by one per
  round. 429s from calls already in flight in the same episode don't halve it again.
- **No herd.** Calls that waited out a cooldown resume spread over up to ``resume_jitter_sec``
  (never longer than the cooldown they waited).

Used from a single event loop (``ModelLoop``), so no locks are needed.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from cfr_v2.telemetry import get_logger, record_concurrency_limit

logger = get_logger(__name__)


class RateGate:
    def __init__(
        self,
        *,
        max_concurrency: int,
        start_concurrency: int | None = None,
        requests_per_minute: float | None = None,
        cooldown_sec: float = 5.0,
        cooldown_max_sec: float = 60.0,
        resume_jitter_sec: float = 2.0,
        name: str = "chat",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if start_concurrency is not None and start_concurrency < 1:
            raise ValueError("start_concurrency must be at least 1")
        self.name = name
        self.max_concurrency = max_concurrency
        self._limit = float(min(start_concurrency or max_concurrency, max_concurrency))
        self._threshold = float(max_concurrency)  # doubling below it (slow start), +1 a round above
        self._in_flight = 0
        self._round = 0  # successes since the allowance last grew
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._cooldown_sec = cooldown_sec
        self._cooldown_max_sec = cooldown_max_sec
        self._resume_jitter_sec = resume_jitter_sec
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._next_start = 0.0
        self._cooldown_until = 0.0
        self._strikes = 0
        record_concurrency_limit(self.name, self.limit)

    @property
    def limit(self) -> int:
        """Calls allowed at once right now."""
        return int(self._limit)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one call slot: after any cooldown, under the current allowance, at the paced rate."""
        await self._wait_cooldown()
        await self._acquire()
        try:
            await self._wait_cooldown()  # a 429 may have landed while this call queued for a slot
            await self._pace()
            yield
        finally:
            self._in_flight -= 1
            self._wake()

    def rate_limited(self, retry_after_sec: float | None = None) -> float:
        """Record a 429 and return the cooldown it imposes on every caller.

        429s from calls already in flight during a cooldown are the same episode:
        they may lengthen it to their Retry-After but don't escalate it, or halve the allowance again.
        """
        now = self._clock()
        if now >= self._cooldown_until:
            self._strikes += 1
            before = self.limit
            self._threshold = max(1.0, self._limit / 2)
            self._limit, self._round = self._threshold, 0
            if self.limit != before:
                logger.warning("llm: %s rate limited: allowing %d calls at once, was %d", self.name, self.limit, before)
                record_concurrency_limit(self.name, self.limit)
        delay = min(self._cooldown_max_sec, self._cooldown_sec * 2 ** (self._strikes - 1))
        if retry_after_sec:
            delay = max(delay, retry_after_sec)
        self._cooldown_until = max(self._cooldown_until, now + delay)
        return self._cooldown_until - now

    def resume_by(self, cooldown: float) -> float:
        """The longest a call waits out ``cooldown``: the cooldown, plus the most jitter after it.
        Callers check it against their deadline before waiting."""
        return cooldown + min(self._resume_jitter_sec, cooldown)

    def succeeded(self) -> None:
        """A call got through: after a round of these, allow more at once."""
        self._strikes = 0
        if self._limit >= self.max_concurrency:
            return
        self._round += 1
        if self._round < self.limit:
            return
        self._round = 0
        if self._limit < self._threshold:  # slow start: double, up to where the last 429 struck
            grown = min(self._limit * 2, self._threshold)
        else:  # past it: one more per round
            grown = self._limit + 1
        self._limit = float(min(self.max_concurrency, grown))
        if self.limit == self.max_concurrency:
            logger.info("llm: %s allowing %d calls at once, the cap", self.name, self.limit)
        record_concurrency_limit(self.name, self.limit)
        self._wake()

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self._clock())

    async def _acquire(self) -> None:
        loop = asyncio.get_running_loop()
        while self._in_flight >= self.limit:
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                elif waiter.done() and not waiter.cancelled():
                    self._wake()  # it was woken for a free slot: hand that slot on
                raise
        self._in_flight += 1

    def _wake(self) -> None:
        free = self.limit - self._in_flight
        while free > 0 and self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                free -= 1

    async def _wait_cooldown(self) -> None:
        waited = 0.0
        while (wait := self._cooldown_until - self._clock()) > 0:
            waited += wait
            await self._sleep(wait)
        spread = min(self._resume_jitter_sec, waited)  # no herd at its end; never longer than the wait
        if spread > 0:
            await self._sleep(self._jitter(0.0, spread))

    async def _pace(self) -> None:
        if not self._interval:
            return
        now = self._clock()
        start = max(now, self._next_start)
        self._next_start = start + self._interval
        if start > now:
            await self._sleep(start - now)
