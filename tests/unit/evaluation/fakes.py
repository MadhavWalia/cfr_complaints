"""Fakes for the evaluation tests: runs, stored results and a reader, without a database or a store."""

from __future__ import annotations

from datetime import UTC, datetime

from openpyxl import load_workbook

from cfr_v2.evaluation import workbook as wb
from cfr_v2.evaluation.source import Results, Run, Transcript


class FakeReader:
    def __init__(self, transcript: Transcript | None = None, baseline=None):
        self._transcript, self._baseline = transcript, baseline

    def transcript(self, results):
        return self._transcript

    def baseline(self, results):
        return self._baseline


def run(case_no: str = "C-1", run_id: str = "run-1") -> Run:
    return Run(run_id, case_no, datetime(2026, 11, 20, 9, tzinfo=UTC))


def results(**parsed) -> Results:
    """Stored results by task, e.g. results(**{"c04.routing": {...}}): refs are made up."""
    return Results(run=None, parsed=parsed, refs={task: f"sha256:{i}" for i, task in enumerate(parsed)})


class RoundTrip:
    """Export rows to a workbook, fill in review answers, read it back and score it."""

    def __init__(self, tmp_path, stage):
        self.path, self.stage = tmp_path / f"{stage.key}.xlsx", stage

    def export(self, rows, choices=None):
        wb.write(
            self.path,
            self.stage.sheets(choices if choices is not None else {}),
            rows,
            lambda: [("Stage", self.stage.title)],
        )
        return load_workbook(self.path)

    def answer(self, sheet: str, by_row: dict[int, dict[str, object]]):
        """Answers by Excel row number and header."""
        book = load_workbook(self.path)
        ws = book[sheet]
        headers = {c.value: c.column for c in ws[1]}
        for line, answers in by_row.items():
            for header, value in answers.items():
                ws.cell(row=line, column=headers[header], value=value)
        book.save(self.path)

    def score(self):
        return self.stage.score(wb.read(self.path, self.stage.sheets()))

    def rows(self, sheet: str):
        book = load_workbook(self.path)
        ws = book[sheet]
        headers = [c.value for c in ws[1]]
        return [dict(zip(headers, (c.value for c in r), strict=False)) for r in ws.iter_rows(min_row=2)]


def find(report, title):
    """A report table's rows by its title, cells as plain values (a rate as its value)."""
    table = next(t for t in report.tables if t.title == title)
    return [[c.value if hasattr(c, "hits") else c for c in row] for row in table.rows]
