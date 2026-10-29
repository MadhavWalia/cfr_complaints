"""Lane workers for this process (LDR-0011).

Per lane: one poller claims at most as many jobs as there are free slots, and a
fixed set of worker threads runs them, so ``<lane>_queue_concurrency`` caps the
lane per process (deployment-wide: per process × processes). One heartbeat
thread renews the leases of every job in flight here. All threads are daemons;
a process that dies simply stops renewing, and its jobs are reclaimed by any
other process once their leases expire.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Iterable

from cfr_v2.contracts import Lane
from cfr_v2.telemetry import get_logger

from . import jobs
from .context import Runtime
from .workflow import run_job

logger = get_logger(__name__)


class LaneWorker:
    def __init__(self, rt: Runtime, lane: Lane, concurrency: int) -> None:
        self.rt = rt
        self.lane = lane
        self.concurrency = concurrency
        self._jobs: queue.Queue[jobs.ClaimedJob | None] = queue.Queue()
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._work, name=f"cfr-{lane.value}-{i}", daemon=True) for i in range(concurrency)
        ]
        self._poller = threading.Thread(target=self._poll, name=f"cfr-{lane.value}-poll", daemon=True)

    def start(self) -> None:
        for t in self._threads:
            t.start()
        self._poller.start()

    def wake(self) -> None:
        self._wake.set()

    def inflight(self) -> list[str]:
        with self._lock:
            return list(self._inflight)

    def stop(self, timeout: float) -> list[str]:
        """Stop claiming; wait up to ``timeout`` for running jobs; return those still running."""
        self._stop.set()
        self._wake.set()
        for _ in self._threads:
            self._jobs.put(None)
        for t in self._threads:
            t.join(timeout=timeout if timeout > 0 else 0.01)
        self._poller.join(timeout=1)
        return self.inflight()

    def _poll(self) -> None:
        settings = self.rt.settings
        while not self._stop.is_set():
            with self._lock:
                free = self.concurrency - len(self._inflight)
            claimed: list[jobs.ClaimedJob] = []
            if free > 0:
                try:
                    with self.rt.db.transaction() as tx:
                        claimed = jobs.claim(
                            tx, self.lane, owner=settings.executor_id, limit=free, lease_sec=settings.lease_sec
                        )
                except Exception:  # database unavailable; keep polling
                    logger.exception("runner: claim failed on lane %s", self.lane.value)
                with self._lock:
                    self._inflight.update(j.case_run_id for j in claimed)
                for job in claimed:
                    self._jobs.put(job)
            if claimed and len(claimed) == free:
                continue  # lane may have more work; claim again as soon as a slot frees
            self._wake.wait(settings.queue_polling_interval_sec)
            self._wake.clear()

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                run_job(self.rt, job)
            except Exception:  # settling the job itself failed; the lease will expire and it is reclaimed
                logger.exception("runner: job %s could not be settled", job.case_run_id)
            finally:
                with self._lock:
                    self._inflight.discard(job.case_run_id)
                self._wake.set()


class Runner:
    def __init__(self, rt: Runtime, lanes: Iterable[Lane]) -> None:
        self.rt = rt
        concurrency = {Lane.BATCH: rt.settings.batch_queue_concurrency, Lane.REALTIME: rt.settings.realtime_queue_concurrency}
        self.lanes = {lane: LaneWorker(rt, lane, concurrency[lane]) for lane in lanes}
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._beat, name="cfr-heartbeat", daemon=True)

    def start(self) -> None:
        for worker in self.lanes.values():
            worker.start()
        if self.lanes:
            self._heartbeat.start()

    def wake(self, lane: Lane) -> None:
        worker = self.lanes.get(lane)
        if worker is not None:
            worker.wake()

    def stop(self, timeout: float = 0) -> None:
        self._stop.set()
        unfinished = [run_id for worker in self.lanes.values() for run_id in worker.stop(timeout)]
        if unfinished:
            try:
                with self.rt.db.transaction() as tx:
                    released = jobs.release(tx, unfinished, owner=self.rt.settings.executor_id)
                logger.info("runner: released %d unfinished job(s) for other workers", released)
            except Exception:  # leases expire on their own
                logger.exception("runner: could not release unfinished jobs")

    def _beat(self) -> None:
        settings = self.rt.settings
        while not self._stop.wait(settings.heartbeat_sec):
            ids = [run_id for worker in self.lanes.values() for run_id in worker.inflight()]
            if not ids:
                continue
            try:
                with self.rt.db.transaction() as tx:
                    jobs.renew(tx, ids, owner=settings.executor_id, lease_sec=settings.lease_sec)
            except Exception:  # transient; the next beat retries well within the lease
                logger.exception("runner: lease renewal failed")
