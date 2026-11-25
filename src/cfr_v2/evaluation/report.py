"""A stage's metrics: rates with their sample size and 95% range, in tables (LDR-0042 §4, §6).

A rate is ``hits / n``. "Partly" answers add half a hit (Q13), so ``hits`` can be fractional. The
range is the Wilson score interval, which behaves at 0%, 100% and small n, where the textbook
normal interval doesn't. A rate over fewer than 30 rows carries ``small`` (the page's Small sample
badge, Q11).

A ``Report`` is what a score job writes as JSON and the console shows: a headline, tables of
cells (text, numbers or rates), and what was left out and why. It holds counts and labels only,
never text from a case: the reviewers' free text goes to the xlsx copy, which stays local.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

SMALL = 30
Z95 = 1.959963984540054


@dataclass(frozen=True)
class Rate:
    hits: float
    n: float  # rows counted; fractional only when Partly answers are in it (estimated recall)

    @property
    def value(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        """The Wilson score interval at 95%."""
        if not self.n:
            return None
        p, n, z = self.hits / self.n, self.n, Z95
        centre = (p + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        return max(0.0, centre - half), min(1.0, centre + half)

    @property
    def small(self) -> bool:
        return self.n < SMALL

    def json(self) -> dict[str, Any]:
        low, high = self.interval or (None, None)
        return {
            "rate": None if self.value is None else round(self.value, 4),
            "low": None if low is None else round(low, 4),
            "high": None if high is None else round(high, 4),
            "n": round(self.n, 2),
            "hits": round(self.hits, 2),
            "small": self.small,
        }

    def __str__(self) -> str:
        if self.value is None:
            return "no rows"
        low, high = self.interval or (0, 0)
        return f"{self.value:.1%} (n = {self.n:g}, {low:.1%}–{high:.1%})"


def rate(hits: float, n: float) -> Rate:
    return Rate(hits, n)


def share(answers: Iterable[str | None], *, yes: str = "Y", partly: str | None = "Partly") -> Rate:
    """Of the answered values: ``yes`` counts 1, ``partly`` a half, anything else 0."""
    given = [a for a in answers if a is not None]
    hits = sum(1.0 if a == yes else 0.5 if partly is not None and a == partly else 0.0 for a in given)
    return Rate(hits, len(given))


def strict(answers: Iterable[str | None], *, yes: str = "Y") -> Rate:
    return share(answers, yes=yes, partly=None)


Cell = str | int | float | Rate | None


@dataclass
class Table:
    title: str
    columns: Sequence[str]
    rows: list[Sequence[Cell]] = field(default_factory=list)
    note: str = ""  # one line under it, e.g. how a rate is counted
    sortable: bool = False  # the console lets the reader sort it (DLS Sortable data table)

    def add(self, *cells: Cell) -> None:
        self.rows.append(cells)

    def json(self) -> dict[str, Any]:
        def cell(c: Cell) -> Any:
            return c.json() if isinstance(c, Rate) else c

        return {
            "title": self.title,
            "columns": list(self.columns),
            "rows": [[cell(c) for c in row] for row in self.rows],
            "note": self.note,
            "sortable": self.sortable,
        }


@dataclass
class Report:
    stage: str
    headline_label: str
    headline: Rate
    tables: list[Table] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # rows, reviewed, not reviewed, skipped, stale
    skipped: list[dict[str, Any]] = field(default_factory=list)  # sheet, row, reason
    about: dict[str, Any] = field(default_factory=dict)  # release, reviewer, export id, versions
    texts: list[Table] = field(default_factory=list)  # tables holding reviewers' text: xlsx only

    def json(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "headline": {"label": self.headline_label, **self.headline.json()},
            "tables": [t.json() for t in self.tables],
            "counts": self.counts,
            "skipped": self.skipped,
            "about": self.about,
        }

    def line(self) -> str:
        """What the job prints last, and the journal shows."""
        return f"{self.headline_label} {self.headline}"


def mix(values: Iterable[str | None], title: str, label: str, *, order: Sequence[str] = ()) -> Table:
    """How often each value occurs: count and share."""
    given = [v for v in values if v]
    counts: dict[str, int] = {}
    for v in given:
        counts[v] = counts.get(v, 0) + 1
    names = [o for o in order if o in counts] + sorted(k for k in counts if k not in order)
    table = Table(title, (label, "Count", "Share"))
    for name in names:
        table.add(name, counts[name], Rate(counts[name], len(given)))
    return table


def spread(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    """Mean, median and 90th percentile (nearest rank), or Nones."""
    if not values:
        return None, None, None
    ordered = sorted(values)
    p90 = ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]
    return round(sum(ordered) / len(ordered), 2), median(ordered), p90
