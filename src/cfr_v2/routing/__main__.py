"""C04 trial: route the claims of C02 trial review files, outside the workflow.

    python -m cfr_v2.routing sha256:<snapshot> --workdir lightrag_store \\
        --verticals config/verticals/knowledge_document.txt --claims-from c02_trial --out router_trial
    python -m cfr_v2.routing publish-verticals config/verticals/knowledge_document.txt

``publish-verticals`` stores the knowledge document as an INTERNAL artifact and prints its ref,
for a release manifest's ``routing_verticals`` (LDR-0021). It prints counts and refs only.

Per claim: one LightRAG search (``mix``; one gpt-5 call for its keywords) and one router call.
Writes nothing to the state database or the artifact store. Prints counts, time and tokens
only. ``--out`` gets one review file per case (the claim, where it went, the reason, and the
sections shown and relied on) and ``routing.xlsx`` to mark each decision right or wrong.
**Both hold claim and procedure text: keep them on this machine and delete them after review.**
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cfr_v2.contracts import ClaimRouting, ClaimRoutingOutcome
from cfr_v2.llm import ModelError, ModelRequest, ModelResponse
from cfr_v2.telemetry import get_logger

from . import route
from .router import ROUTE_VERSION, ClaimInput, Router, Section
from .verticals import VerticalCatalog

logger = get_logger(__name__)
OUTCOMES = (ClaimRoutingOutcome.UNMAPPED.value, ClaimRoutingOutcome.NO_ELIGIBLE_VERTICAL.value)
REVIEW_HEADERS = (
    "claim_no",
    "case_id",
    "claim_id",
    "claim",
    "routed_to",
    "confidence",
    "reason",
    "sections relied on",
    "correct",
    "should_be",
    "notes",
)
HOW_TO_REVIEW = (
    "Each row is one customer claim and where the router sent it.",
    "In 'correct', choose Yes or No. When No, choose in 'should_be' where it should have gone.",
    "UNMAPPED: a concrete American Express failure that no vertical covers.",
    "NO_ELIGIBLE_VERTICAL: no failure in American Express's handling is alleged.",
    "Use 'notes' for anything else, such as a reason that is right for the wrong vertical.",
    "This file holds claim and procedure text: keep it local and delete it when you're done.",
)


@dataclass
class CaseClaims:
    case_id: str
    claims: list[ClaimInput]


@dataclass
class Routed:
    case_id: str
    claim: ClaimInput
    sections: list[Section] = field(default_factory=list)
    routing: ClaimRouting | None = None
    error: str | None = None
    seconds: float = 0.0

    @property
    def decision(self) -> str:
        return "FAILED" if self.routing is None else route.decision(self.routing)


class Metered:
    """Counts the router's tokens while delegating to the gateway client."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.input_tokens = self.output_tokens = self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.inner.complete(request)
        self.calls += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        return response


def load_cases(folder: Path, limit: int | None = None) -> list[CaseClaims]:
    """Every claim of every C02 trial review file, in file name order."""
    cases = []
    for path in sorted(folder.glob("*.json")):
        review = json.loads(path.read_text(encoding="utf-8"))
        allegations = {a["allegation_id"]: a for a in review.get("allegations", ())}
        claims = [ClaimInput.from_review(c, allegations) for c in review.get("claims") or ()]
        if claims:
            cases.append(CaseClaims(review["case_id"], claims))
        if limit is not None and len(cases) == limit:
            break
    return cases


async def route_claim(case_id: str, claim: ClaimInput, search, router: Router, lm) -> Routed:
    """One claim, as the stage routes it; an error is recorded and the run goes on."""
    routed = Routed(case_id, claim)
    started = time.monotonic()
    try:
        done = await route.route_claim(case_id, claim, search, router, lm)
        routed.sections, routed.routing = list(done.sections), done.routing
    except ModelError as exc:
        routed.error = type(exc).__name__
    routed.seconds = time.monotonic() - started
    procedures = len({s.hit.source_sha256 for s in routed.sections})
    if routed.error:
        logger.warning("route: case %s claim %s failed (%s)", case_id, claim.claim_id, routed.error)
    else:
        confidence = routed.routing.candidates[0].confidence if routed.routing.candidates else None
        logger.info(
            "route: case %s claim %s: %s%s; %d sections from %d procedures; %.0fs",
            case_id,
            claim.claim_id,
            routed.decision,
            f" ({confidence})" if confidence is not None else "",
            len(routed.sections),
            procedures,
            routed.seconds,
        )
    return routed


async def route_all(cases: Sequence[CaseClaims], search, router: Router, lm, concurrency: int) -> list[Routed]:
    gate = asyncio.Semaphore(concurrency)

    async def one(case_id: str, claim: ClaimInput) -> Routed:
        async with gate:
            return await route_claim(case_id, claim, search, router, lm)

    return list(await asyncio.gather(*(one(c.case_id, claim) for c in cases for claim in c.claims)))


def _relied_on(routed: Routed) -> list[str]:
    if routed.routing is None or not routed.routing.candidates:
        return []
    return [
        f"{c.file_name}: {c.section_title} (pp. {c.page_start}-{c.page_end})"
        for c in routed.routing.candidates[0].procedures
    ]


def write_reviews(results: Sequence[Routed], out: Path, catalog: VerticalCatalog, procedures_ref: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    by_case: dict[str, list[Routed]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)
    for case_id, routed in by_case.items():
        review = {
            "case_id": case_id,
            "program_version": ROUTE_VERSION,
            "procedures_ref": procedures_ref,
            "verticals_sha256": catalog.sha256,
            "claims": [
                {
                    "claim_id": r.claim.claim_id,
                    "statement": r.claim.statement,
                    "complaints": list(r.claim.complaints),
                    "routed_to": r.decision,
                    "error": r.error,
                    "routing": r.routing and r.routing.model_dump(mode="json"),
                    "sections_shown": [
                        {
                            "id": s.section_id,
                            "file_name": s.hit.file_name,
                            "section": s.hit.section_title,
                            "pages": f"{s.hit.page_start}-{s.hit.page_end}",
                        }
                        for s in r.sections
                    ],
                    "seconds": round(r.seconds, 1),
                }
                for r in routed
            ],
        }
        name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in case_id)
        (out / f"{name}.json").write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_workbook(results: Sequence[Routed], path: Path, catalog: VerticalCatalog) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook()
    guide = book.active
    guide.title = "how to review"
    for line in HOW_TO_REVIEW:
        guide.append([line])
    guide.column_dimensions["A"].width = 110
    sheet = book.create_sheet("routing")
    sheet.append(REVIEW_HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for number, r in enumerate(results, start=1):
        confidence = ""
        if r.routing is not None and r.routing.candidates:
            confidence = {0.9: "high", 0.6: "medium", 0.3: "low"}.get(r.routing.candidates[0].confidence, "")
        sheet.append(
            [
                number,
                r.case_id,
                r.claim.claim_id,
                r.claim.statement,
                r.decision,
                confidence,
                (r.routing.rationale if r.routing else r.error) or "",
                "\n".join(_relied_on(r)),
                None,
                None,
                None,
            ]
        )
    widths = {"A": 9, "B": 16, "C": 9, "D": 60, "E": 26, "F": 11, "G": 70, "H": 60, "I": 9, "J": 26, "K": 40}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    last = len(results) + 1
    correct = DataValidation(type="list", formula1='"Yes,No"', allow_blank=True)
    should = DataValidation(type="list", formula1=f'"{",".join((*catalog.ids, *OUTCOMES))}"', allow_blank=True)
    for rule, column in ((correct, "I"), (should, "J")):
        sheet.add_data_validation(rule)
        rule.add(f"{column}2:{column}{last}")
    sheet.freeze_panes = "A2"
    book.save(path)


def print_summary(results: Sequence[Routed], metered: Metered, search_calls: int, seconds: float) -> None:
    decisions = Counter(r.decision for r in results)
    claims = len(results)
    print(f"{claims} claims routed in {seconds:.0f}s; {decisions.get('FAILED', 0)} failed")
    for decision, count in sorted(decisions.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {decision:<44}{count:>5}")
    shown = [len(r.sections) for r in results if r.routing]
    empty = sum(1 for r in results if r.routing and not r.sections)
    if empty:
        print(f"{empty} claims routed without any procedure sections (search blocked or found nothing)")
    cited = [len(r.routing.candidates[0].procedures) for r in results if r.routing and r.routing.candidates]
    if shown:
        print(f"sections shown per claim: {sum(shown) / len(shown):.1f}; relied on when routed: ", end="")
        print(f"{sum(cited) / len(cited):.1f}" if cited else "n/a")
    confidence = Counter(c.confidence for r in results if r.routing for c in r.routing.candidates)
    print(
        "confidence: "
        + ", ".join(f"{k} {confidence.get(v, 0)}" for k, v in (("high", 0.9), ("medium", 0.6), ("low", 0.3)))
    )
    if metered.calls:
        print(
            f"router calls {metered.calls}: {metered.input_tokens / metered.calls:,.0f} input and "
            f"{metered.output_tokens / metered.calls:,.0f} output tokens per call; search model calls {search_calls}"
        )


def run(args: argparse.Namespace) -> int:
    from cfr_v2.llm import GatewayEmbeddingClient, GatewayModelClient, GatewaySettings, model_loop
    from cfr_v2.llm.dspy_lm import gateway_lm
    from cfr_v2.procedures import corpus_passages
    from cfr_v2.procedures.lightrag_engine import GatewayLlm, LightRagProcedureSearch, open_rag

    catalog = VerticalCatalog.load(args.verticals)
    cases = load_cases(args.claims_from, args.cases)
    if not cases:
        print(f"error: no claims in {args.claims_from}", file=sys.stderr)
        return 1
    print(
        f"{sum(len(c.claims) for c in cases)} claims from {len(cases)} cases; {len(catalog.ids)} verticals "
        f"(definitions sha256:{catalog.sha256[:12]})",
        file=sys.stderr,
    )
    settings = GatewaySettings.from_env()
    metered = Metered(GatewayModelClient(settings))
    lm = gateway_lm(metered, settings.model_id)
    passages = corpus_passages(_store(), args.procedures)
    llm = GatewayLlm(GatewayModelClient(settings))
    router = Router(catalog)

    async def go() -> list[Routed]:
        async with open_rag(args.workdir, GatewayEmbeddingClient(), llm) as rag:
            search = LightRagProcedureSearch(rag, args.procedures, passages, llm)
            return await route_all(cases, search, router, lm, args.concurrency)

    started = time.monotonic()
    results = model_loop().run(go())
    print_summary(results, metered, llm.calls, time.monotonic() - started)
    if args.out:
        write_reviews(results, args.out, catalog, args.procedures)
        write_workbook(results, args.out / "routing.xlsx", catalog)
        print(f"review files and {args.out / 'routing.xlsx'} written: they hold claim text, so keep them local")
    return 0 if all(r.routing for r in results) else 2


def _store():
    from cfr_v2.artifacts import S3ArtifactStore, S3Settings

    return S3ArtifactStore(S3Settings.from_env())


def _register(stored) -> None:
    from cfr_v2.artifacts import register
    from cfr_v2.runtime import RuntimeSettings
    from cfr_v2.state import CaseStateRepository, Database

    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=1)
    try:
        with db.transaction() as tx:
            register(CaseStateRepository(tx), stored)
    finally:
        db.close()


def publish_verticals(argv: list[str]) -> int:
    from cfr_v2.contracts import Classification

    parser = argparse.ArgumentParser(
        prog="python -m cfr_v2.routing publish-verticals",
        description="store the knowledge document as an artifact, for a release manifest",
    )
    parser.add_argument("path", type=Path, help="the knowledge document")
    args = parser.parse_args(argv)
    catalog = VerticalCatalog.load(args.path)  # refuses a document without usable verticals
    stored = _store().put(
        catalog.text.encode("utf-8"), media_type="text/markdown", classification=Classification.INTERNAL
    )
    _register(stored)
    print(f"{len(catalog.ids)} verticals; stored as {stored.metadata.ref}")
    print("In the release manifest (registry/releases/<release>.yaml):")
    print(f"routing_verticals: {stored.metadata.ref}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["publish-verticals"]:
        return publish_verticals(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.routing", description=__doc__.splitlines()[0])
    parser.add_argument("procedures", help="the ref the LightRAG store was built from: snapshot, or old index")
    parser.add_argument("--workdir", type=Path, required=True, help="the LightRAG working directory")
    parser.add_argument("--verticals", type=Path, required=True, help="the knowledge document defining the verticals")
    parser.add_argument("--claims-from", type=Path, required=True, help="folder of C02 trial review files")
    parser.add_argument("--cases", type=int, help="route only the first N cases")
    parser.add_argument("--concurrency", type=int, default=4, help="claims routed at once (default 4)")
    parser.add_argument("--out", type=Path, help="directory for review files and routing.xlsx")
    args = parser.parse_args(argv)

    from cfr_v2.llm import close_model_loop
    from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

    init_telemetry(service_name="cfr-v2-routing")
    try:
        return run(args)
    finally:
        close_model_loop()
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
