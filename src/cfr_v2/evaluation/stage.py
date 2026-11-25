"""What every stage's evaluation provides (LDR-0042 §5), and the columns they share."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .report import Report
from .source import Reader, Results, Run
from .workbook import Column, Review, Row, Sheet

REF = Column(
    "Ref",
    24,
    hidden=True,
    note="Hidden. The case run and item this row is, so the upload matches it exactly. Don't delete it.",
)
NOTES = Column("Notes", 40, wrap=True, review=True, note="For the reviewer: anything worth saying. Never scored.")
CASE_NO = Column("Case no", 16)


@dataclass(frozen=True)
class ExportContext:
    """What an export knows besides the runs: the release, and where to read its pins."""

    release: str
    registry: Path
    store: Any


class Stage(Protocol):
    key: str  # the tab and file names: "routing"
    title: str  # "Routing (C04)"
    task: str  # the stage whose success puts a run in the export: "c04.routing"
    tasks: tuple[str, ...]  # the results a row needs
    headline: str  # the headline metric's name

    def sheets(self, choices: Mapping[str, tuple[str, ...]] | None = None) -> tuple[Sheet, ...]:
        """The workbook's sheets. ``choices`` fill dropdowns that depend on the release; ``None``
        (when reading) leaves those columns unchecked."""

    def choices(self, ctx: ExportContext, rows_seen: Iterable[Row]) -> Mapping[str, tuple[str, ...]]: ...

    def rows(self, run: Run, results: Results, reader: Reader) -> Iterable[Row]: ...

    def score(self, review: Review) -> Report: ...


def stamp(run: Run, results: Results, task: str) -> str:
    """Which stored result a row came from: the run and the artifact, for the stale check."""
    return f"{run.case_run_id} {results.refs.get(task, '')}"


def ref(run: Run, item: str = "") -> str:
    return f"{run.case_run_id}/{item}" if item else run.case_run_id


def lines(items: Iterable[str | None]) -> str:
    return "\n".join(i for i in items if i)


def answered(rows: Iterable[Any], header: str) -> list[Any]:
    return [r for r in rows if r[header] is not None]
