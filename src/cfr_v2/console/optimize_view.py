"""The Optimize area's own sections (LDR-0027 phase 3): one row per prompt, the runs, and one
run's results. Read from the console's jobs and their ``report.json``; nothing is run here.
Reports hold case ids, scores and instructions, never case text."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from .jobs import Job, JobRunner, JobStatus


@dataclass
class Run:
    job: Job
    prompt_id: str
    report: Any = None  # optimize.report.Report, once the job wrote one

    @property
    def baseline(self) -> float | None:
        return _mean(self.report.baseline) if self.report and self.report.baseline else None

    @property
    def candidate(self) -> float | None:
        return _mean(self.report.candidate) if self.report and self.report.candidate else None


@dataclass
class PromptRow:
    prompt: Any  # optimize.prompts.Prompt
    version: str
    gold_sets: list[Job] = field(default_factory=list)  # checked gold sets, newest first
    runs: list[Run] = field(default_factory=list)

    @property
    def last_run(self) -> Run | None:
        return self.runs[0] if self.runs else None


def _mean(scores: dict[str, float]) -> float:
    return sum(scores.values()) / len(scores)


def _prompt_of(job: Job) -> str:
    return (job.form or {}).get("prompt", "")


def load_run(runner: JobRunner, job: Job) -> Run:
    from cfr_v2.optimize.report import Report

    path = runner.folder(job.id) / "outputs" / "run" / "report.json"
    report = Report.from_json(path.read_text(encoding="utf-8")) if path.is_file() else None
    return Run(job, _prompt_of(job), report)


def overview(runner: JobRunner) -> list[PromptRow]:
    from cfr_v2.optimize.prompts import PROMPTS

    jobs = runner.list()
    rows = {p.prompt_id: PromptRow(p, p.source_version()) for p in PROMPTS}
    for job in jobs:
        row = rows.get(_prompt_of(job))
        if row is None:
            continue
        if job.command == "optimize.check" and job.status is JobStatus.DONE:
            row.gold_sets.append(job)
        elif job.command == "optimize.run":
            row.runs.append(load_run(runner, job))
    return list(rows.values())


@dataclass(frozen=True)
class Line:
    kind: str  # "same", "added", "removed"
    text: str


def instruction_diff(before: str, after: str) -> list[Line]:
    """The instructions line by line, marking what the candidate added and removed."""
    out = []
    for line in difflib.ndiff(before.strip().splitlines(), after.strip().splitlines()):
        tag, text = line[:2], line[2:]
        if tag == "? ":
            continue
        out.append(Line({"+ ": "added", "- ": "removed"}.get(tag, "same"), text))
    return out


__all__ = ["Line", "PromptRow", "Run", "instruction_diff", "load_run", "overview"]
