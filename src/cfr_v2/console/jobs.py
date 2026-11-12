"""Jobs (LDR-0023 decision 4): each console action runs one pipeline command as a subprocess.

A job is a folder, ``<workspace>/jobs/<id>/``:

- ``job.json``: what ran and how it ended (rewritten atomically on every change);
- ``log.txt``: everything the command printed, stdout and stderr together;
- ``inputs/``: files uploaded for it; ``outputs/``: files it wrote, to download.

The process starts in its own session, so stopping or restarting the console never kills a
long build or a worker. A restarted console adopts a job whose process is still alive and marks
one whose process is gone as ``lost``: its exit code was never seen.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from cfr_v2.telemetry import get_logger

from .diagnosis import failure_reason

logger = get_logger("cfr_v2.console.jobs")
ADOPTED_POLL_SEC = 2.0


class JobStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    LOST = "lost"  # the console lost track of the process: its outcome is unknown

    @property
    def pill(self) -> str:
        """The status pill's class: one vocabulary across the console."""
        return {"running": "running", "done": "done", "failed": "failed", "stopped": "waiting", "lost": "lost"}[self]


@dataclass(frozen=True)
class Job:
    id: str
    command: str  # the catalogue entry, e.g. "intake.synthetic"
    title: str  # what the journal shows, e.g. "Intake · 3 synthetic cases, seed 9"
    argv: tuple[str, ...]
    status: JobStatus
    created_at: str
    ended_at: str | None = None
    exit_code: int | None = None
    pid: int | None = None
    stop_requested: bool = False
    summary: str | None = None  # one line for the journal's output column, set by the command
    reason: str | None = None  # why it failed, from the end of its log (diagnosis.failure_reason)
    form: dict[str, str] | None = None  # the form it was started from, for "Run again" (no files)

    @property
    def finished(self) -> bool:
        return self.status is not JobStatus.RUNNING

    @property
    def seconds(self) -> float:
        start = datetime.fromisoformat(self.created_at)
        end = datetime.fromisoformat(self.ended_at) if self.ended_at else datetime.now(UTC)
        return max(0.0, (end - start).total_seconds())

    def to_json(self) -> str:
        body = asdict(self) | {"argv": list(self.argv), "status": self.status.value}
        return json.dumps(body, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Job:
        body = json.loads(text)
        return cls(**(body | {"argv": tuple(body["argv"]), "status": JobStatus(body["status"])}))


PROGRESS_TAIL = 16_000
_PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")  # "[12/35] Fees.pdf" (snapshot), "lightrag: [480/1844] passages …" (build)
_LEFT = re.compile(r"about (\d[\w ]*?) left")


@dataclass(frozen=True)
class Progress:
    done: int
    total: int
    left: str | None = None  # "2 h 10 min", as the command estimates it

    @property
    def percent(self) -> int:
        return min(100, round(100 * self.done / self.total)) if self.total else 0


def read_progress(log: Path) -> Progress | None:
    """The last ``[n/N]`` a running command printed (and its "about … left"), if it prints any."""
    try:
        with log.open("rb") as f:
            f.seek(max(0, f.seek(0, 2) - PROGRESS_TAIL))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if match := _PROGRESS.search(line):
            done, total = int(match.group(1)), int(match.group(2))
            left = _LEFT.search(line)
            return Progress(done, total, left.group(1) if left else None) if total else None
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_job_id() -> str:
    """Sortable by time, unique enough for one machine: ``20261112-165004-3fa9``."""
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, someone else's
        return True
    return True


@dataclass
class _Tracked:
    process: subprocess.Popen | None  # None for a job adopted after a restart


@dataclass
class JobRunner:
    """Starts, tracks and stops jobs under ``root`` (``<workspace>/jobs``)."""

    root: Path
    cwd: Path
    env: Mapping[str, str] = field(repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _tracked: dict[str, _Tracked] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._adopt()

    # --- reading -------------------------------------------------------------------------

    def folder(self, job_id: str) -> Path:
        if not job_id.replace("-", "").isalnum():
            raise KeyError(job_id)
        return self.root / job_id

    def get(self, job_id: str) -> Job:
        path = self.folder(job_id) / "job.json"
        if not path.is_file():
            raise KeyError(job_id)
        return Job.from_json(path.read_text(encoding="utf-8"))

    def list(self, limit: int | None = None) -> list[Job]:
        jobs = []
        for folder in sorted((p for p in self.root.iterdir() if p.is_dir()), reverse=True):
            try:
                jobs.append(Job.from_json((folder / "job.json").read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError):
                continue  # a half-written or foreign folder
            if limit is not None and len(jobs) == limit:
                break
        return jobs

    def files(self, job_id: str, kind: str) -> list[tuple[str, int]]:
        """``(relative path, size)`` of a job's ``inputs`` or ``outputs``, sorted."""
        base = self.folder(job_id) / kind
        if not base.is_dir():
            return []
        return sorted((str(p.relative_to(base)), p.stat().st_size) for p in base.rglob("*") if p.is_file())

    def file(self, job_id: str, kind: str, relative: str) -> Path:
        """A job's file, refusing anything outside its ``inputs`` or ``outputs``."""
        if kind not in ("inputs", "outputs"):
            raise KeyError(kind)
        base = (self.folder(job_id) / kind).resolve()
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise KeyError(relative)
        return path

    def log_path(self, job_id: str) -> Path:
        return self.folder(job_id) / "log.txt"

    # --- running -------------------------------------------------------------------------

    def start(
        self,
        command: str,
        title: str,
        argv: Callable[[Path], Sequence[str]],
        *,
        summarize: Callable[[Path, int], str | None] | None = None,
        form: Mapping[str, str] | None = None,
    ) -> Job:
        """Start a new job. ``argv(folder)`` builds the arguments once the job's folder exists,
        so uploads can be saved into ``inputs/`` and outputs pointed at ``outputs/``.
        ``summarize(folder, exit_code)`` gives the journal's one-line outcome at the end."""
        job_id = new_job_id()
        folder = self.folder(job_id)
        (folder / "inputs").mkdir(parents=True)
        (folder / "outputs").mkdir()
        job = Job(
            job_id, command, title, tuple(argv(folder)), JobStatus.RUNNING, _now(), form=dict(form) if form else None
        )
        argv = list(job.argv)
        log = (folder / "log.txt").open("wb")
        try:
            process = subprocess.Popen(  # argv built from the catalogue, never a shell string
                argv,
                cwd=self.cwd,
                env={**self.env, "PYTHONUNBUFFERED": "1"},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log.write(f"could not start: {type(exc).__name__}\n".encode())
            log.close()
            reason = f"could not start: {type(exc).__name__}"
            job = replace(job, status=JobStatus.FAILED, ended_at=_now(), exit_code=-1, reason=reason)
            self._save(job)
            return job
        log.close()  # the child holds its own copy
        job = replace(job, pid=process.pid)
        with self._lock:
            self._tracked[job_id] = _Tracked(process)
        self._save(job)
        logger.info("console: job %s started (%s)", job_id, command)
        threading.Thread(
            target=self._wait, args=(job_id, process, summarize), daemon=True, name=f"job-{job_id}"
        ).start()
        return job

    def stop(self, job_id: str, *, force: bool = False) -> Job:
        """SIGTERM to the job's process group (a worker drains first), or SIGKILL with ``force``."""
        job = self.get(job_id)
        if job.finished or not job.pid:
            return job
        job = replace(job, stop_requested=True)
        self._save(job)  # before the signal, so a quick exit is recorded as stopped, not failed
        try:
            os.killpg(job.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        logger.info("console: job %s %s", job_id, "killed" if force else "asked to stop")
        return job

    def delete(self, job_id: str) -> None:
        """A finished job's folder, with its log, uploads and outputs (review files are deleted
        once reviewed). A running job must be stopped first: ``RuntimeError``."""
        if not self.get(job_id).finished:
            raise RuntimeError(f"job {job_id} is running")
        shutil.rmtree(self.folder(job_id))
        logger.info("console: job %s deleted", job_id)

    def _wait(self, job_id: str, process: subprocess.Popen, summarize) -> None:
        code = process.wait()
        job = self.get(job_id)
        status = JobStatus.DONE if code == 0 else JobStatus.STOPPED if job.stop_requested else JobStatus.FAILED
        summary = None
        if summarize is not None:
            try:
                summary = summarize(self.folder(job_id), code)
            except Exception as exc:  # noqa: BLE001 - a summary is a nicety; the job's outcome stands
                logger.warning("console: job %s summary failed: %s", job_id, type(exc).__name__)
        reason = failure_reason(self.log_path(job_id)) if status is JobStatus.FAILED else None
        self._save(replace(job, status=status, ended_at=_now(), exit_code=code, summary=summary, reason=reason))
        with self._lock:
            self._tracked.pop(job_id, None)
        logger.info("console: job %s %s (exit %d)", job_id, status.value, code)

    def _adopt(self) -> None:
        """After a restart: keep watching jobs still alive; mark the rest lost."""
        for job in self.list():
            if job.status is not JobStatus.RUNNING:
                continue
            if _alive(job.pid):
                self._tracked[job.id] = _Tracked(None)
                threading.Thread(target=self._watch, args=(job.id,), daemon=True, name=f"adopted-{job.id}").start()
            else:
                self._save(replace(job, status=JobStatus.LOST, ended_at=_now()))

    def _watch(self, job_id: str) -> None:
        job = self.get(job_id)
        while _alive(job.pid):
            time.sleep(ADOPTED_POLL_SEC)
        job = self.get(job_id)
        status = JobStatus.STOPPED if job.stop_requested else JobStatus.LOST
        self._save(replace(job, status=status, ended_at=_now()))
        with self._lock:
            self._tracked.pop(job_id, None)

    def _save(self, job: Job) -> None:
        path = self.folder(job.id) / "job.json"
        tmp = path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(job.to_json(), encoding="utf-8")
            tmp.replace(path)
