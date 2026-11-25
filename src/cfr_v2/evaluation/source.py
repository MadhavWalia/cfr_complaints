"""Which case runs an export covers, and their stored results (LDR-0042 §3, §5).

**The runs.** One per case: its latest run under the release, among those whose stage succeeded,
admitted in the window when one is given. Then one of the three choices of the export form (Q5):
every such case, a random sample of N (a seeded order, so the same seed gives the same sample), or
the cases listed.

**The results.** Read as the case page reads them (``console.cases``): only artifacts the run's own
stages wrote, or that its Pass 1 result names (the transcript), and only INTERNAL ones. So the
redacted transcript can be read, never the original, the redaction map, the notes or the baseline's
identifiers. Read-only, so an export is safe while a worker runs.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cfr_v2.console import cases
from cfr_v2.contracts import Classification

LATEST_RUNS_SQL = (
    "SELECT case_run_id, source_case_id, created_at FROM ("
    " SELECT DISTINCT ON (c.source_case_id) r.case_run_id, c.source_case_id, r.created_at"
    ' FROM case_run r JOIN "case" c ON c.case_id = r.case_id'
    " JOIN release_manifest rm ON rm.manifest_ref = r.release_manifest_ref"
    " WHERE rm.release_id = %(release)s"
    " AND EXISTS (SELECT 1 FROM task_execution t WHERE t.case_run_id = r.case_run_id"
    " AND t.logical_task_id = %(task)s AND t.status = 'SUCCEEDED')"
    " AND (%(start)s::timestamptz IS NULL OR r.created_at >= %(start)s)"
    " AND (%(end)s::timestamptz IS NULL OR r.created_at < %(end)s)"
    " AND (%(cases)s::text[] IS NULL OR c.source_case_id = ANY(%(cases)s))"
    " ORDER BY c.source_case_id, r.created_at DESC, r.case_run_id"
    ") latest"
    " ORDER BY CASE WHEN %(seed)s::text IS NULL THEN NULL ELSE md5(case_run_id || %(seed)s::text) END,"
    " created_at, case_run_id"
    " LIMIT %(limit)s"
)
CURRENT_SQL = (  # the latest successful result of a stage, per run: to tell which exported rows went stale
    "SELECT DISTINCT ON (case_run_id) case_run_id, output_artifact_refs->>0 FROM task_execution"
    " WHERE case_run_id = ANY(%(runs)s) AND logical_task_id = %(task)s AND status = 'SUCCEEDED'"
    " ORDER BY case_run_id, created_at DESC"
)
MIN_CASES, MAX_CASES = 10, 500


@dataclass(frozen=True)
class Choice:
    """The export form's "which cases": all of them, a random sample, or a list."""

    sample: int | None = None
    seed: str | None = None
    case_nos: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        return "chosen" if self.case_nos else "sample" if self.sample else "all"

    def with_seed(self) -> Choice:
        """A random sample always has a seed, made up when none was given, so it can be repeated."""
        if self.sample and not self.seed:
            return Choice(self.sample, secrets.token_hex(4), self.case_nos)
        return self

    def describe(self) -> str:
        if self.case_nos:
            return f"{len(self.case_nos)} cases listed"
        if self.sample:
            return f"a random sample of {self.sample} (seed {self.seed})"
        return "every case"


@dataclass(frozen=True)
class Run:
    case_run_id: str
    case_no: str
    admitted: datetime


@dataclass
class Selection:
    runs: list[Run]
    not_found: list[str] = field(default_factory=list)  # listed cases with no run that fits


def select_runs(
    db, *, task: str, release: str, start: datetime | None, end: datetime | None, choice: Choice
) -> Selection:
    params = {
        "task": task,
        "release": release,
        "start": start,
        "end": end,
        "cases": list(choice.case_nos) or None,
        "seed": choice.seed if choice.sample else None,
        "limit": choice.sample,
    }
    with db.transaction() as tx:
        runs = [Run(r[0], r[1], r[2]) for r in tx.all(LATEST_RUNS_SQL, params)]
    found = {r.case_no for r in runs}
    return Selection(runs, [c for c in choice.case_nos if c not in found])


def current_refs(db, task: str, case_run_ids: Sequence[str]) -> dict[str, str]:
    """Each run's latest successful result of ``task`` now."""
    if not case_run_ids:
        return {}
    with db.transaction() as tx:
        return {r[0]: r[1] for r in tx.all(CURRENT_SQL, {"runs": list(case_run_ids), "task": task})}


@dataclass
class Results:
    """One run's stage results (parsed JSON), and the refs they came from."""

    run: cases.CaseRun
    parsed: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)

    def get(self, task: str) -> Any:
        return self.parsed.get(task)


class Reader:
    """Reads runs' results and redacted transcripts, INTERNAL only."""

    def __init__(self, db, store):
        self.db, self.store = db, store

    def results(self, case_run_id: str, tasks: Sequence[str]) -> Results | None:
        run = cases.read_case(self.db, case_run_id)
        if run is None:
            return None
        found = Results(run)
        for stage in run.stages:  # oldest first: the latest success wins
            if stage.task in tasks and stage.status == "SUCCEEDED" and stage.refs:
                found.parsed[stage.task] = cases.parse_result(
                    cases.read_result(self.db, self.store, run, stage.refs[0])
                )
                found.refs[stage.task] = stage.refs[0]
        return found

    def baseline(self, results: Results) -> Any:
        """The baseline profile the run's Pass 2 was given, or None."""
        pass2 = results.get("c02.pass2")
        try:
            return cases.parse_result(cases.read_baseline(self.db, self.store, results.run, pass2))
        except cases.NotShown:
            return None

    def _internal(self, ref: str) -> bytes:
        if not isinstance(ref, str) or not ref.startswith("sha256:"):
            raise cases.NotShown(str(ref))
        with self.db.transaction() as tx:
            stored = set(tx.column(cases.CLASSIFICATIONS_SQL, {"sha": ref.removeprefix("sha256:")}))
        if Classification.INTERNAL.value not in stored:
            raise cases.NotShown(ref)
        return self.store.get(ref, classification=Classification.INTERNAL, authorization=cases.READER)

    def transcript(self, results: Results) -> Transcript | None:
        """The redacted transcript the run's Pass 1 read: only the ref its own result names."""
        pass1 = results.get("c02.pass1")
        ref = pass1.get("transcript_ref") if isinstance(pass1, dict) else None
        if not ref:
            return None
        canonical = json.loads(self._internal(ref))
        texts, quality, sources, length = {}, None, 0, 0
        for interaction in canonical.get("interactions", ()):
            texts[interaction["interaction_id"]] = self._internal(interaction["redacted_text_ref"]).decode("utf-8")
            quality = quality or interaction.get("linkage_quality")
            sources += len(interaction.get("source_interaction_ids") or ())
            length += int(interaction.get("redacted_length") or 0)
        return Transcript(texts, quality, sources, length)


@dataclass(frozen=True)
class Transcript:
    texts: dict[str, str]  # interaction id -> redacted text
    linkage_quality: str | None  # intake's exact_match_flag
    calls: int  # how many transcripts intake joined
    length: int  # characters, redacted

    @property
    def text(self) -> str:
        return "\n\n".join(self.texts.values())

    def quote(self, span: dict[str, Any]) -> str:
        body = self.texts.get(span.get("interaction_id", ""), "")
        return body[int(span.get("start", 0)) : int(span.get("end", 0))]
