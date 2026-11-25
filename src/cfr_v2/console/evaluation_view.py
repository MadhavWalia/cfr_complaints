"""The Evaluation page by stage (LDR-0042): its tabs, each stage's review instructions, and the scores
read back from finished score jobs. Reads job folders only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .jobs import Job, JobRunner, JobStatus

HISTORY_ROWS = 10


@dataclass(frozen=True)
class StageView:
    key: str
    tab: str  # the tab's label: 1-2 words, as the DLS asks
    title: str  # the page heading: the tab's words and the stage code
    does: str  # one line: what the stage does and what the review asks
    review: tuple[str, ...] = ()  # how to review, step by step
    built: bool = True  # False: nothing to review yet (C05)


STAGES = (
    StageView(
        "intake",
        "Intake",
        "Intake (C01)",
        "Intake finds each case's call transcripts and joins them. The review asks: is the transcript this case's call?",
        (
            "Read the start of each transcript. Right call? is Y when it is the customer's call about this complaint, N when it isn't, Can't tell when the start isn't enough.",
            "Extra calls? is Y when unrelated calls are mixed in. Problem names what's wrong when something is.",
            "Cases without a transcript are listed for the counts; leave them empty.",
        ),
    ),
    StageView(
        "pass1",
        "Pass 1",
        "Pass 1 (C02)",
        "Pass 1 lists the customer's allegations and requests and the agent's statements, each with its quotes. The review asks: is each one said, and right?",
        (
            "On items, judge each row: Correct? (Y, Partly or N), and the right type if it's wrong. Quotes support it? says whether its quotes back it up.",
            "When an item shouldn't be there, say why in Should not be here because.",
            "On cases, give how many of each list were missed (0 when none) and, briefly, what. That is what estimates recall.",
        ),
    ),
    StageView(
        "pass2",
        "Pass 2",
        "Pass 2 (C02)",
        "Pass 2 groups the allegations into claims and grounds them: account, event window, evidence needs. The review asks: are the claims right, and how they're grounded?",
        (
            "On claims, judge each claim (Y, Partly or N), its grouping, its account, its event window and its evidence needs. Pick a missing evidence source when one is.",
            "On cases, give how many claims were missed (0 when none), and whether the other checks are right.",
        ),
    ),
    StageView(
        "selection",
        "Selection",
        "Selection (C03)",
        "C03 picks the claim the customer most wants put right, or answers AMBIGUOUS. The review asks: is that the main complaint?",
        (
            "Correct? is Y when the answer is right. For AMBIGUOUS, Y means the claims really are equally central.",
            "When it's N, give the right primary: a claim number, Unclear, or No eligible claim.",
            "Cases with one claim are decided without the model: they're scored on their own, not in the headline.",
        ),
    ),
    StageView(
        "routing",
        "Routing",
        "Routing (C04)",
        "C04 routes each claim to the vertical whose procedures apply. The review asks: is the vertical right for the claim?",
        (
            "Correct? is Y when the vertical is right. When it's N, pick the right one and say why in a sentence.",
            "Sections useful? says whether the procedure sections cited support the decision.",
            "Every rate counts claims, not cases.",
        ),
    ),
    StageView(
        "planning",
        "Planning",
        "Planning (C05)",
        "Planning isn't built yet: C05 writes a placeholder plan, so there is nothing to review. Its review appears here once C05 writes real plans.",
        built=False,
    ),
)
BY_KEY = {s.key: s for s in STAGES}


@dataclass(frozen=True)
class Score:
    job: Job
    report: dict[str, Any] | None  # None when the job failed or its file can't be read
    files: tuple[str, ...] = ()  # its outputs, for download links

    @property
    def headline(self) -> dict[str, Any] | None:
        return (self.report or {}).get("headline")


def _report(runner: JobRunner, job: Job, key: str) -> dict[str, Any] | None:
    if job.status is not JobStatus.DONE:
        return None
    try:
        return json.loads(runner.file(job.id, "outputs", f"{key}_metrics.json").read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError):
        return None


def scores(runner: JobRunner, key: str, jobs: list[Job] | None = None) -> list[Score]:
    """Every score job of a stage, newest first."""
    found = []
    for job in jobs if jobs is not None else runner.list():
        if job.command == f"evaluation.{key}_score":
            files = tuple(name for name, _ in runner.files(job.id, "outputs"))
            found.append(Score(job, _report(runner, job, key), files))
    return found


def exports(
    runner: JobRunner, key: str, jobs: list[Job] | None = None, limit: int = 5
) -> list[tuple[Job, tuple[str, ...]]]:
    """The stage's latest export jobs and their workbooks."""
    found = []
    for job in jobs if jobs is not None else runner.list():
        if job.command == f"evaluation.{key}_export":
            found.append((job, tuple(n for n, _ in runner.files(job.id, "outputs") if n.endswith(".xlsx"))))
            if len(found) == limit:
                break
    return found


def latest(found: list[Score]) -> Score | None:
    return next((s for s in found if s.report is not None), None)


def overview(runner: JobRunner) -> list[tuple[StageView, Score | None]]:
    jobs = runner.list()
    return [(stage, latest(scores(runner, stage.key, jobs)) if stage.built else None) for stage in STAGES]
