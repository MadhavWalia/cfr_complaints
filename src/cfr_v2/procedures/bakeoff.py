"""The retrieval bake-off (LDR-0019): which engine finds the procedure sections a claim needs?

Real claim statements go to both engines. Their passages go into one workbook, unlabelled and in
random order, for someone who knows the procedures to judge. Which engine found what stays in a
separate key file, so the judge can't favour one, and scoring joins the two.

The workbook holds claim statements and procedure text, both redacted: keep it with the gold set's
care, and delete it after scoring. The key holds numbers only.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cfr_v2.contracts import ProcedureHit, ProcedureSearchRequest

from .search import ProcedureSearch

SHEET = "judgements"
HEADERS = ("item", "claim_no", "claim", "procedure", "section", "pages", "passage", "relevant", "notes")
RELEVANT, PARTLY, NOT_RELEVANT = "Yes", "Partly", "No"
INSTRUCTIONS = (
    "Thank you for helping compare two ways of finding procedure sections.",
    "",
    "Each row on the 'judgements' sheet is a customer's claim and one passage from a procedure.",
    "In the 'relevant' column, choose one answer for every row:",
    "  Yes: this passage would help decide how the claim should be handled, or which team owns it.",
    "  Partly: it's on the right topic, but wouldn't help decide.",
    "  No: it doesn't help with this claim.",
    "Judge each passage by itself. A claim can have many Yes rows, or none.",
    "The rows are in random order on purpose. Please don't sort or delete them, or change the first column.",
    "Add a note if a claim needs a procedure that never appears.",
    "This file holds claim text: keep it local and delete it when you're done.",
)


@dataclass(frozen=True)
class BakeoffClaim:
    claim_no: int
    case_id: str
    statement: str


def load_claims(folder: Path, count: int, *, seed: int = 7) -> list[BakeoffClaim]:
    """Claims from C02 trial review files: each case's primary claim first, then one per case in
    turn, so a few talkative cases can't fill the set."""
    per_case: list[tuple[str, list[str]]] = []
    for path in sorted(folder.glob("*.json")):
        review = json.loads(path.read_text(encoding="utf-8"))
        claims = review.get("claims") or []
        primary = (review.get("selection") or {}).get("claim_id")
        ordered = sorted(claims, key=lambda c: c["claim_id"] != primary)
        if ordered:
            per_case.append((review["case_id"], [c["statement"] for c in ordered]))
    random.Random(seed).shuffle(per_case)
    chosen: list[BakeoffClaim] = []
    seen: set[str] = set()
    for turn in range(max((len(s) for _, s in per_case), default=0)):
        for case_id, statements in per_case:
            if len(chosen) == count:
                return chosen
            if turn < len(statements) and statements[turn] not in seen:
                seen.add(statements[turn])
                chosen.append(BakeoffClaim(len(chosen) + 1, case_id, statements[turn]))
    return chosen


@dataclass
class BakeoffItem:
    item: int
    claim: BakeoffClaim
    hit: ProcedureHit
    ranks: dict[str, int] = field(default_factory=dict)  # engine -> rank


async def collect(
    claims: Sequence[BakeoffClaim], engines: Mapping[str, ProcedureSearch], *, top: int = 5, seed: int = 7
) -> list[BakeoffItem]:
    """One item per distinct passage per claim, shuffled within the claim."""
    generator = random.Random(seed)
    items: list[BakeoffItem] = []
    for claim in claims:
        found: dict[tuple[str, int], BakeoffItem] = {}
        for name, engine in engines.items():
            request = ProcedureSearchRequest(query=claim.statement, limit=top, redacted=True)
            result = await engine.search(request)
            for rank, hit in enumerate(result.hits, start=1):
                key = (hit.source_sha256, hit.section_index)
                found.setdefault(key, BakeoffItem(0, claim, hit)).ranks[name] = rank
        passages = list(found.values())
        generator.shuffle(passages)
        items += passages
    for number, item in enumerate(items, start=1):
        item.item = number
    return items


def _pages(hit: ProcedureHit) -> str:
    return str(hit.page_start) if hit.page_start == hit.page_end else f"{hit.page_start}-{hit.page_end}"


def write_workbook(items: Sequence[BakeoffItem], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook()
    guide = book.active
    guide.title = "how to judge"
    for line in INSTRUCTIONS:
        guide.append([line])
    guide.column_dimensions["A"].width = 110
    sheet = book.create_sheet(SHEET)
    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for entry in items:
        hit = entry.hit
        sheet.append(
            [
                entry.item,
                entry.claim.claim_no,
                entry.claim.statement,
                hit.file_name,
                hit.section_title,
                _pages(hit),
                hit.text,
                None,
                None,
            ]
        )
    for column, width in zip("ABCDEFGHI", (7, 9, 45, 35, 30, 9, 90, 10, 30), strict=True):
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    choice = DataValidation(type="list", formula1=f'"{RELEVANT},{PARTLY},{NOT_RELEVANT}"', allow_blank=True)
    sheet.add_data_validation(choice)
    choice.add(f"H2:H{max(len(items) + 1, 2)}")
    sheet.freeze_panes = "A2"
    book.save(path)


def write_key(items: Sequence[BakeoffItem], engines: Sequence[str], top: int, path: Path) -> None:
    key = {
        "engines": list(engines),
        "top": top,
        "items": {
            str(i.item): {
                "claim_no": i.claim.claim_no,
                "source_sha256": i.hit.source_sha256,
                "file_name": i.hit.file_name,
                "ranks": i.ranks,
            }
            for i in items
        },
    }
    path.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")


@dataclass
class EngineScore:
    engine: str
    claims: int = 0
    hit_rate: float = 0.0  # claims with at least one Yes
    precision: float = 0.0  # share of shown passages judged Yes
    partly_or_better: float = 0.0
    first_relevant: float = 0.0  # mean reciprocal rank of the first Yes
    only_here: int = 0  # Yes passages the other engine didn't show
    procedures_per_claim: float = 0.0


@dataclass
class BakeoffReport:
    judged: int
    unjudged: int
    unknown_items: int
    scores: list[EngineScore]

    def as_dict(self) -> dict:
        return asdict(self)


def read_judgements(path: Path) -> dict[int, str]:
    from openpyxl import load_workbook

    sheet = load_workbook(path, read_only=True)[SHEET]
    rows = sheet.iter_rows(min_row=1, values_only=True)
    header = list(next(rows))
    item, relevant = header.index("item"), header.index("relevant")
    answers: dict[int, str] = {}
    for row in rows:
        if row[item] is not None:
            answers[int(row[item])] = str(row[relevant] or "").strip()
    return answers


def score(answers: Mapping[int, str], key: Mapping) -> BakeoffReport:
    items = key["items"]
    engines = key["engines"]
    usable = {n: a for n, a in answers.items() if str(n) in items and a in (RELEVANT, PARTLY, NOT_RELEVANT)}
    unknown = sum(str(n) not in items for n in answers)
    claims = {entry["claim_no"] for n, entry in items.items() if int(n) in usable}
    scores = []
    for engine in engines:
        shown: dict[int, list[tuple[int, str, str]]] = defaultdict(list)
        only = 0
        for number, entry in items.items():
            answer = usable.get(int(number))
            rank = entry["ranks"].get(engine)
            if answer is None or rank is None:
                continue
            shown[entry["claim_no"]].append((rank, answer, entry["source_sha256"]))
            only += answer == RELEVANT and len(entry["ranks"]) == 1
        rows = [row for group in shown.values() for row in group]
        first = [
            next((1 / rank for rank, answer, _ in sorted(group) if answer == RELEVANT), 0.0)
            for group in shown.values()
        ]
        total = len(claims) or 1
        scores.append(
            EngineScore(
                engine=engine,
                claims=len(shown),
                hit_rate=round(sum(v > 0 for v in first) / total, 3),
                precision=round(sum(a == RELEVANT for _, a, _ in rows) / (len(rows) or 1), 3),
                partly_or_better=round(sum(a != NOT_RELEVANT for _, a, _ in rows) / (len(rows) or 1), 3),
                first_relevant=round(sum(first) / total, 3),
                only_here=only,
                procedures_per_claim=round(sum(len({s for *_, s in g}) for g in shown.values()) / total, 2),
            )
        )
    return BakeoffReport(len(usable), len(items) - len(usable), unknown, scores)
