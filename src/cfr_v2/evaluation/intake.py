"""Intake (C01): is the transcript this case's call? (LDR-0042 §5.1)

**Workbook** (sheet ``cases``, one row per case): when it was admitted, how many calls intake joined
into its transcript, its length, whether the call's agent matched (intake's ``exact_match_flag``),
and the first 1,500 characters of the redacted transcript (Q3); for the reviewer, Right call?,
Extra calls? and Problem. No notes, case source or category (Q8): the reviewer judges the
transcript on its own.

A run is in the export once Pass 1 has run: its result names the transcript (or says there is none).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .report import Rate, Report, Table, mix, share, spread
from .source import Reader, Results, Run
from .stage import CASE_NO, NOTES, REF, ExportContext, ref, stamp
from .workbook import YN, Column, Review, Row, Sheet

SHEET = "cases"
START = 1500
RIGHT = ("Y", "N", "Can't tell")
PROBLEMS = ("Wrong customer", "Unrelated call", "Several calls, one right", "Empty or garbled", "Other")


class Intake:
    key = "intake"
    title = "Intake (C01)"
    task = "c02.pass1"
    tasks = ("c02.pass1",)
    headline = "Right call"

    def sheets(self, choices: Mapping[str, tuple[str, ...]] | None = None) -> tuple[Sheet, ...]:
        return (
            Sheet(
                SHEET,
                (
                    CASE_NO,
                    Column("Admitted", 12),
                    Column(
                        "Calls joined",
                        10,
                        note="How many call transcripts intake found for the case and joined into one",
                    ),
                    Column("Characters", 11, note="The redacted transcript's length"),
                    Column(
                        "Agent matched",
                        12,
                        note="Intake's exact_match_flag: whether the call's agent is the one in the call record",
                    ),
                    Column(
                        "Transcript start",
                        100,
                        wrap=True,
                        note=f"The first {START:,} characters of the redacted transcript",
                    ),
                    Column(
                        "Right call?",
                        12,
                        review=True,
                        choices=RIGHT,
                        note="For the reviewer: is this the customer's call about this complaint?",
                    ),
                    Column(
                        "Extra calls?",
                        12,
                        review=True,
                        choices=YN,
                        note="For the reviewer: are unrelated calls mixed in?",
                    ),
                    Column("Problem", 24, review=True, choices=PROBLEMS),
                    NOTES,
                    REF,
                ),
                key=("Ref",),
            ),
        )

    def choices(self, ctx: ExportContext, rows_seen: Iterable[Row]) -> Mapping[str, tuple[str, ...]]:
        return {}

    def rows(self, run: Run, results: Results, reader: Reader) -> Iterable[Row]:
        transcript = reader.transcript(results)
        if transcript is None:
            values = (run.case_no, run.admitted.date().isoformat(), 0, 0, "", "(no transcript)")
        else:
            body = transcript.text
            start = body[:START] + ("…" if len(body) > START else "")
            values = (
                run.case_no,
                run.admitted.date().isoformat(),
                transcript.calls,
                transcript.length,
                str(transcript.linkage_quality or ""),
                start,
            )
        yield Row(SHEET, (*values, ref(run)), stamp(run, results, self.task))

    def score(self, review: Review) -> Report:
        rows = review.rows[SHEET]
        with_text = [r for r in rows if r["Transcript start"] != "(no transcript)"]
        reviewed = [r for r in with_text if r["Right call?"] is not None]
        judged = [r for r in reviewed if r["Right call?"] in ("Y", "N")]
        report = Report(self.key, self.headline, share((r["Right call?"] for r in judged), partly=None))
        report.counts = {
            "cases": len(rows),
            "without a transcript": len(rows) - len(with_text),
            "reviewed": len(reviewed),
            "not reviewed": len(with_text) - len(reviewed),
            "can't tell": len(reviewed) - len(judged),
        }

        main = Table("Transcripts", ("Measure", "Value"))
        main.add("Right call", report.headline)
        clean = [r for r in judged if r["Extra calls?"] is not None]
        main.add(
            "Right call and nothing else mixed in",
            Rate(sum(1 for r in clean if r["Right call?"] == "Y" and r["Extra calls?"] == "N"), len(clean)),
        )
        main.add("Can't tell", Rate(len(reviewed) - len(judged), len(reviewed)))
        main.note = "Right call counts Y and N answers; Can't tell is shown on its own."
        report.tables.append(main)
        report.tables.append(mix((r["Problem"] for r in reviewed), "Problems", "Problem", order=PROBLEMS))

        matched = Table("By agent match", ("Agent matched", "Cases", "Right call"))
        for value in sorted({r["Agent matched"] or "" for r in judged}):
            group = [r for r in judged if (r["Agent matched"] or "") == value]
            matched.add(value or "(not recorded)", len(group), share((r["Right call?"] for r in group), partly=None))
        report.tables.append(matched)

        calls = Table("By calls joined", ("Calls joined", "Cases", "Right call"))
        for label, keep in (
            ("1", lambda n: n == 1),
            ("2", lambda n: n == 2),
            ("3", lambda n: n == 3),
            ("4 or more", lambda n: n >= 4),
        ):
            group = [r for r in judged if keep(int(r["Calls joined"] or 0))]
            calls.add(label, len(group), share((r["Right call?"] for r in group), partly=None))
        report.tables.append(calls)

        auto = Table("From the export (no review needed)", ("Measure", "Value"))
        auto.add("Cases without a transcript", Rate(len(rows) - len(with_text), len(rows)))
        mean, mid, p90 = spread([float(r["Calls joined"] or 0) for r in with_text])
        auto.add("Calls joined per case: mean", mean)
        auto.add("Calls joined per case: 90th percentile", p90)
        mean, mid, p90 = spread([float(r["Characters"] or 0) for r in with_text])
        auto.add("Characters per case: median", mid)
        auto.add("Characters per case: 90th percentile", p90)
        report.tables.append(auto)
        return report
