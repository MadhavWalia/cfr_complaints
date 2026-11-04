"""Score Pass 1 against the gold set (Phase C exit), and make blank gold workbooks.

    python -m cfr_v2.understanding.score template --case-no GC-1 --case-no GC-2 gold_template.xlsx
    python -m cfr_v2.understanding.score template --cases-file cases.txt gold_template.xlsx
    python -m cfr_v2.understanding.score run gold_AB_2026-11-10.xlsx gold_CD_2026-11-10.xlsx
    python -m cfr_v2.understanding.score run gold_*.xlsx --run-missing --out pass1_score.json

``run`` reads the labellers' workbooks, takes each case's committed ``c02.pass1`` result
from the state database (with ``--run-missing``, runs Pass 1 now for cases without one,
writing nothing), and prints recall, precision, F1, type, ``about`` and link agreement
per list, a breakdown by ``prim_ctgy``, the pipeline's dropped-item rate, and the
agreement between labellers on cases labelled twice. Where the gold ``cases`` sheet names
the main complaint and the run has committed ``c02.pass2`` and ``c03`` results, it also
prints C03's top-1 accuracy and how it used ``AMBIGUOUS``. When a case is in several
workbooks, the first workbook given is the one the pipeline is scored against.

Output holds counts, case numbers and workbook rows only: no transcript or gold text.
Needs RESTRICTED read access (gold quotes are located in the original transcript).
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cfr_v2.artifacts import OverlayArtifactStore, S3ArtifactStore, S3Settings
from cfr_v2.contracts import (
    AuthorizationContext,
    CanonicalTranscript,
    ClaimSetRevision,
    Classification,
    IngestionRequest,
    Pass1Result,
    PrimarySelection,
    RedactionMap,
)
from cfr_v2.runtime.settings import RuntimeSettings
from cfr_v2.runtime.stages import STAGES, StageInput
from cfr_v2.state import CaseStateRepository, Database

from .gold import GoldCase, GoldError, read_gold, write_template
from .scoring import LISTS, CaseText, ListScore, Report, gold_side, pipeline_side, score_selection, score_sides

_EVALUATOR = AuthorizationContext(
    principal="c02.evaluation",
    purpose="gold-set scoring",
    allowed_classifications=frozenset({"RESTRICTED", "INTERNAL"}),
)


def case_text(store, transcript_ref: str) -> CaseText:
    def get(ref: str, classification: Classification) -> bytes:
        return store.get(ref, classification=classification, authorization=_EVALUATOR)

    transcript = CanonicalTranscript.model_validate_json(get(transcript_ref, Classification.INTERNAL))
    segments, originals = [], []
    for i in transcript.interactions:
        segments.append((i.interaction_id, get(i.redacted_text_ref, Classification.INTERNAL).decode("utf-8")))
        originals.append(
            (
                i.interaction_id,
                get(i.original_text_ref, Classification.RESTRICTED).decode("utf-8"),
                RedactionMap.model_validate_json(get(i.redaction_map_ref, Classification.RESTRICTED)),
            )
        )
    return CaseText(tuple(segments), tuple(originals))


def _latest_runs(db: Database, case_nos: list[str]) -> dict[str, tuple[str, str, str]]:
    """case_no -> (case_run_id, request_ref, release_manifest_ref) of its latest ingestion revision."""
    with db.transaction() as tx:
        rows = tx.all(
            "SELECT c.source_case_id, r.case_run_id, r.request_ref, r.release_manifest_ref FROM case_run r"
            ' JOIN "case" c USING (case_id) WHERE c.source_case_id = ANY(%s) ORDER BY r.ingestion_revision',
            (case_nos,),
        )
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def _committed(db: Database, store, run_id: str, task: str, model):
    """The run's committed result for ``task`` (latest), parsed as ``model``; None if there is none."""
    with db.transaction() as tx:
        done = [c for c in CaseStateRepository(tx).committed_results(run_id) if c.logical_task_id == task]
    if not done:
        return None
    data = store.get(done[-1].output_artifact_refs[0], classification=Classification.INTERNAL, authorization=_EVALUATOR)
    return model.model_validate_json(data)


def _run_pass1(store, runs: dict[str, tuple[str, str, str]], case_nos: list[str]) -> dict[str, Pass1Result]:
    from cfr_v2.llm import GatewayModelClient, GatewaySettings, ModelError, close_model_loop
    from cfr_v2.llm.dspy_lm import gateway_lm

    from .handler import make_pass1_handler

    settings = GatewaySettings.from_env()
    handler = make_pass1_handler(store, gateway_lm(GatewayModelClient(settings), settings.model_id))

    def one(case_no: str) -> tuple[str, Pass1Result | None]:
        run_id, request_ref, manifest_ref = runs[case_no]
        try:
            out = handler(StageInput(run_id, STAGES[0], request_ref, (), release_manifest_ref=manifest_ref))
        except ModelError as exc:
            print(f"  {case_no}: Pass 1 failed ({type(exc).__name__})", file=sys.stderr)
            return case_no, None
        return case_no, Pass1Result.model_validate_json(out.content)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return {case_no: result for case_no, result in pool.map(one, case_nos) if result is not None}
    finally:
        close_model_loop()


def score(
    gold_paths: list[Path],
    *,
    run_missing: bool = False,
    min_overlap: float = 0.3,
    db: Database | None = None,
    store=None,
) -> Report:
    """Score every labelled case. ``db`` and ``store`` default to the environment's (``CFR_*``, ``S3_*``)."""
    by_case: dict[str, list[GoldCase]] = {}
    for path in gold_paths:
        for case in read_gold(path):
            by_case.setdefault(case.case_no, []).append(case)

    report = Report()
    labelled = {c: v for c, v in by_case.items() if not v[0].could_not_label}
    report.cases_not_labelled = sorted(c for c in by_case if c not in labelled)

    own_db = db is None
    db = db or Database(RuntimeSettings.from_env().state_database_url, max_connections=2)
    store = OverlayArtifactStore(store or S3ArtifactStore(S3Settings.from_env()))  # --run-missing writes nothing
    try:
        runs = _latest_runs(db, sorted(labelled))
        predictions: dict[str, Pass1Result] = {}
        for case_no, (run_id, _, _) in runs.items():
            result = _committed(db, store, run_id, "c02.pass1", Pass1Result)
            if result is not None:
                predictions[case_no] = result
        missing = sorted(c for c in labelled if c in runs and c not in predictions)
        if missing and run_missing:
            print(f"running Pass 1 for {len(missing)} cases without a committed result", file=sys.stderr)
            predictions |= _run_pass1(store, runs, missing)

        for case_no, versions in sorted(labelled.items()):
            if case_no not in runs or case_no not in predictions:
                report.cases_without_prediction.append(case_no)
                continue
            request = IngestionRequest.model_validate_json(
                store.get(runs[case_no][1], classification=Classification.RESTRICTED, authorization=_EVALUATOR)
            )
            if request.transcript_ref is None:
                report.cases_without_prediction.append(case_no)
                continue
            text = case_text(store, request.transcript_ref)
            primary, problems = gold_side(versions[0], text)
            report.quote_problems += problems
            result = predictions[case_no]
            category = request.case_metadata.get("prim_ctgy")
            predicted = pipeline_side(result)
            report.add_case(case_no, score_sides(primary, predicted, min_overlap), category, result)
            # C03: needs the committed claim set and selection (workers, or a full trial run).
            run_id = runs[case_no][0]
            claim_set = _committed(db, store, run_id, "c02.pass2", ClaimSetRevision)
            selection = _committed(db, store, run_id, "c03.primary_selection", PrimarySelection)
            if versions[0].primary is not None:
                if claim_set is None or selection is None or claim_set.allegations != result.allegations:
                    report.cases_without_selection.append(case_no)
                else:
                    report.selection.add(
                        score_selection(versions[0], primary, predicted, claim_set, selection, min_overlap)
                    )
            # Inter-labeller agreement: the first labeller against each other one.
            for other in versions[1:]:
                second, problems = gold_side(other, text)
                report.quote_problems += problems
                for name, s in score_sides(primary, second, min_overlap).items():
                    report.agreement[name].add(s)
                report.agreement_cases.append(case_no)
    finally:
        if own_db:
            db.close()
    return report


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def _table(title: str, scores: dict[str, ListScore]) -> None:
    print(f"\n{title}")
    header = ("gold", "pipeline", "matched", "precision", "recall", "F1", "type", "about", "links", "recall*")
    print(f"{'':<14}" + "".join(f"{h:>10}" for h in header))
    for name, s in scores.items():
        cells = (
            s.gold,
            s.predicted,
            s.matched,
            _pct(s.precision),
            _pct(s.recall),
            _pct(s.f1),
            _pct(s.kind_agreement),
            _pct(s.about_agreement) if name == "allegations" else "",
            _pct(s.link_accuracy),
            _pct(s.unambiguous_recall),
        )
        print(f"{name:<14}" + "".join(f"{c:>10}" for c in cells))


def print_report(report: Report) -> None:
    print(f"cases scored: {len(report.cases_scored)}; Pass 1 versions: {report.program_versions or '-'}")
    if report.cases_without_prediction:
        print(f"no Pass 1 result (or no transcript), not scored: {', '.join(report.cases_without_prediction)}")
    if report.cases_not_labelled:
        print(f"marked could_not_label, not scored: {', '.join(report.cases_not_labelled)}")
    _table("Pipeline against the gold set", report.totals)
    print(
        "  type: same Claim/Impact, request type or statement type among matches; about: same 'about'; "
        "links: requests/statements linked to the right items; recall*: on gold rows not marked ambiguous"
    )
    print(f"  pipeline items dropped for a quote not found in the transcript: {_pct(report.dropped_rate)}")
    if report.allegations_by_category:
        print("\nAllegations by prim_ctgy")
        for category, s in sorted(report.allegations_by_category.items()):
            print(f"  {category:<40} gold {s.gold:>4}  recall {_pct(s.recall):>5}  precision {_pct(s.precision):>5}")
    s = report.selection
    if s.clear or s.unclear or s.unmatched:
        print("\nC03 main complaint against the gold primary_allegation_no")
        print(
            f"  clear cases (primary_unclear = N): {s.clear}; top-1 {_pct(s.top1)}; said AMBIGUOUS {_pct(s.ambiguous_when_clear)}"
        )
        print(f"  unclear cases (primary_unclear = Y): {s.unclear}; said AMBIGUOUS {_pct(s.ambiguous_when_unclear)}")
        if s.unmatched:
            print(f"  gold main complaint not found among the pipeline's allegations: {s.unmatched}")
    if report.cases_without_selection:
        print(
            f"  no committed claim set and selection, not scored for C03: {', '.join(report.cases_without_selection)}"
        )
    if report.agreement_cases:
        _table(
            f"Labeller agreement ({len(set(report.agreement_cases))} cases labelled twice; first as gold)",
            report.agreement,
        )
    if report.quote_problems:
        print(f"\nGold quotes not found in the transcript ({len(report.quote_problems)}): send back to the labeller")
        for p in report.quote_problems:
            print(f"  {p.source}: {p.sheet} row {p.row} (case {p.case_no})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.understanding.score", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    tpl = sub.add_parser("template", help="write an empty gold workbook for a list of cases")
    tpl.add_argument("--case-no", action="append", default=[], help="a case to label (repeatable)")
    tpl.add_argument("--cases-file", type=Path, help="a text file with one case number per line")
    tpl.add_argument("workbook", type=Path)
    run = sub.add_parser("run", help="score Pass 1 against gold workbooks")
    run.add_argument("gold", type=Path, nargs="+", help="labellers' workbooks; the first wins for shared cases")
    run.add_argument("--run-missing", action="store_true", help="run Pass 1 now for cases without a result")
    run.add_argument("--min-overlap", type=float, default=0.3, help="quote overlap needed to match (default 0.3)")
    run.add_argument("--out", type=Path, help="also write the report as JSON")
    args = parser.parse_args(argv)

    if args.command == "template":
        cases = list(args.case_no)
        if args.cases_file:
            cases += [line.strip() for line in args.cases_file.read_text().splitlines() if line.strip()]
        if not cases:
            parser.error("give --case-no or --cases-file")
        write_template(list(dict.fromkeys(cases)), args.workbook)
        print(f"wrote {args.workbook} ({len(set(cases))} cases)")
        return 0
    try:
        report = score(args.gold, run_missing=args.run_missing, min_overlap=args.min_overlap)
    except GoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print_report(report)
    if args.out:
        args.out.write_text(json.dumps(report.as_dict(), indent=2) + "\n")
        print(f"\nreport written to {args.out}")
    return 0


__all__ = ["LISTS", "main", "score"]


if __name__ == "__main__":
    sys.exit(main())
