"""Evidence source taxonomy: registry YAML, with a business-maintained workbook (P4).

Two owners (lld/guides/evidence-sources.md):

- **Source types** (ids, descriptions, examples, deprecations) are what Pass 2's model
  reads. The technical team edits them in ``registry/evidence_sources/<version>.yaml``
  through a PR, as a prompt change: ``check`` lints the text, and any change needs a
  new version file.
- **Backends** (where each source type's evidence lives), **proposals** (new or changed
  source types, in plain words) and the **change log** are kept by the business team in
  a workbook. ``import`` applies the backends to the same version, appends proposals and
  changes to ``<version>.requests.yaml``, and prints a change report for review. It
  refuses a workbook whose (locked) ``source_types`` sheet was edited.

    python -m cfr_v2.registry.evidence_sources export evidence-sources-1 evidence_sources.xlsx
    python -m cfr_v2.registry.evidence_sources import evidence_sources.xlsx
    python -m cfr_v2.registry.evidence_sources check

The workbook needs ``openpyxl`` (dev dependency group); loading YAML does not.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cfr_v2.contracts import AuthorizationContext, EvidenceBackend, EvidenceSourceTaxonomy

from .manifests import DEFAULT_REGISTRY_DIR, load_pinned

DEFAULT_VERSION = "evidence-sources-1"  # used by release manifests that name none
SUBDIR = "evidence_sources"
OTHER = "other"  # catch-all type; exempt from the example rules

TYPE_COLUMNS = ("id", "description", "examples", "deprecated", "replaced_by")
BACKEND_COLUMNS = ("source_type", "system", "dataset", "status", "owner", "notes")
PROPOSAL_COLUMNS = ("kind", "source_type", "what_and_why", "example_from_a_case", "raised_by", "date", "status")
CHANGE_COLUMNS = ("date", "who", "what_changed", "why")
PROPOSAL_KINDS = ("new source type", "change wording", "retire")
STATUSES = ("available", "planned", "none")

DESCRIPTION_CHARS = (20, 300)
MAX_EXAMPLES = 3
_ROWS = 500  # rows covered by dropdowns and duplicate highlighting


class EvidenceSourceError(ValueError):
    pass


# --- YAML ---------------------------------------------------------------------------------


def path_for(version: str, directory: Path = DEFAULT_REGISTRY_DIR) -> Path:
    return directory / SUBDIR / f"{version}.yaml"


def requests_path_for(version: str, directory: Path = DEFAULT_REGISTRY_DIR) -> Path:
    return directory / SUBDIR / f"{version}.requests.yaml"


def versions(directory: Path = DEFAULT_REGISTRY_DIR) -> list[str]:
    return sorted(p.stem for p in (directory / SUBDIR).glob("*.yaml") if not p.stem.endswith(".requests"))


def load_taxonomy(version: str = DEFAULT_VERSION, directory: Path = DEFAULT_REGISTRY_DIR) -> EvidenceSourceTaxonomy:
    path = path_for(version, directory)
    if not path.is_file():
        raise EvidenceSourceError(f"no evidence source taxonomy {version!r} at {path}")
    try:
        taxonomy = EvidenceSourceTaxonomy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as exc:
        raise EvidenceSourceError(f"{path}: {exc}") from None
    if taxonomy.version != version:
        raise EvidenceSourceError(f"{path}: version {taxonomy.version!r} does not match the file name")
    return taxonomy


class PinnedTaxonomies:
    """The taxonomy for a case run's pinned release manifest (P4, ADR-14), cached per manifest ref.

    A manifest that names none, or a run with no manifest ref, gets ``DEFAULT_VERSION``. A
    manifest ref that was never pinned is an error: the run's release is unknown.
    ``db`` may be a callable, for handlers built before the runtime opens its pool.
    """

    _READER = AuthorizationContext(principal="c02.pass2", purpose="release manifest")

    def __init__(self, store: Any, db: Any, directory: Path = DEFAULT_REGISTRY_DIR) -> None:
        self.store = store
        self._db = db
        self.directory = directory
        self._cache: dict[str | None, EvidenceSourceTaxonomy] = {}
        self._lock = threading.Lock()

    def __call__(self, manifest_ref: str | None) -> EvidenceSourceTaxonomy:
        with self._lock:
            cached = self._cache.get(manifest_ref)
        if cached is not None:
            return cached
        version = DEFAULT_VERSION
        if manifest_ref is not None:
            db = self._db() if callable(self._db) else self._db
            manifest = load_pinned(manifest_ref, store=self.store, db=db, authorization=self._READER)
            version = manifest.evidence_sources or DEFAULT_VERSION
        taxonomy = load_taxonomy(version, self.directory)
        with self._lock:
            self._cache[manifest_ref] = taxonomy
        return taxonomy


TaxonomySource = EvidenceSourceTaxonomy | Callable[[str | None], EvidenceSourceTaxonomy]


def to_yaml(taxonomy: EvidenceSourceTaxonomy) -> str:
    body = taxonomy.model_dump(mode="json", exclude_defaults=True)
    header = (
        "# Evidence source taxonomy (P4); guide: lld/guides/evidence-sources.md\n"
        "# Source types: technical team, edited here (a new version for any change). Backends: business\n"
        "# team, via the workbook (`python -m cfr_v2.registry.evidence_sources import <workbook>`).\n"
    )
    return header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=120)


def save(taxonomy: EvidenceSourceTaxonomy, directory: Path = DEFAULT_REGISTRY_DIR) -> Path:
    """Write a version; refuse a change to the model-facing part of an existing one."""
    path = path_for(taxonomy.version, directory)
    if path.is_file() and load_taxonomy(taxonomy.version, directory).taxonomy_hash != taxonomy.taxonomy_hash:
        raise EvidenceSourceError(
            f"{taxonomy.version} already exists with different source types; write a new version file instead"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_yaml(taxonomy), encoding="utf-8")
    return path


def lint(taxonomy: EvidenceSourceTaxonomy) -> list[str]:
    """Text rules for the model-facing part (run by ``check``, so by CI)."""
    problems = []
    seen: dict[str, str] = {}
    low, high = DESCRIPTION_CHARS
    for t in taxonomy.active():
        if not low <= len(t.description) <= high:
            problems.append(f"{t.id}: description should be {low}-{high} characters, not {len(t.description)}")
        if t.id != OTHER and not 1 <= len(t.examples) <= MAX_EXAMPLES:
            problems.append(f"{t.id}: give 1-{MAX_EXAMPLES} examples, not {len(t.examples)}")
        for example in t.examples:
            if not example.rstrip().endswith("?"):
                problems.append(f"{t.id}: example should be a question: {example!r}")
            key = " ".join(example.lower().split())
            if key in seen and seen[key] != t.id:
                problems.append(f"{t.id}: example also used by {seen[key]}: {example!r}")
            seen.setdefault(key, t.id)
    return problems


def _backend_key(b: EvidenceBackend) -> tuple[str, str]:
    return (b.system.lower(), (b.dataset or "").lower())


def _duplicate_backends(taxonomy: EvidenceSourceTaxonomy) -> list[str]:
    out = []
    for t in taxonomy.source_types:
        keys = [_backend_key(b) for b in t.backends]
        out += [
            f"{t.id}: {system} {dataset}".strip() for system, dataset in sorted({k for k in keys if keys.count(k) > 1})
        ]
    return out


# --- workbook -----------------------------------------------------------------------------


def _cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _rows(sheet, columns: tuple[str, ...]) -> list[tuple[int, dict[str, str | None]]]:
    rows = sheet.iter_rows(values_only=True)
    header = [_cell(h) for h in next(rows, ())]
    missing = [c for c in columns if c not in header]
    if missing:
        raise EvidenceSourceError(f"sheet {sheet.title!r}: missing columns {missing}")
    out = []
    for number, row in enumerate(rows, start=2):
        record = {name: _cell(row[i]) if i < len(row) else None for i, name in enumerate(header) if name}
        if any(record.get(c) for c in columns):
            out.append((number, record))
    return out


@dataclass
class Workbook:
    version: str
    source_types: list[dict[str, Any]]  # as shown on the locked sheet
    backends: dict[str, list[dict[str, Any]]]
    proposals: list[dict[str, str | None]]
    changes: list[dict[str, str | None]]


def read_workbook(path: Path) -> Workbook:
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    for name in ("read_me", "source_types", "backends", "proposals", "changes"):
        if name not in book.sheetnames:
            raise EvidenceSourceError(f"{path}: missing sheet {name!r}")
    about = {_cell(k): _cell(v) for k, v, *_ in book["read_me"].iter_rows(values_only=True) if _cell(k)}
    if not about.get("version"):
        raise EvidenceSourceError(f"{path}: the 'read_me' sheet has lost its 'version' row")

    types = [
        {
            "id": r["id"],
            "description": r["description"],
            "examples": tuple(line.strip() for line in (r["examples"] or "").splitlines() if line.strip()),
            "deprecated": (r["deprecated"] or "N").upper() == "Y",
            "replaced_by": r["replaced_by"],
        }
        for _, r in _rows(book["source_types"], TYPE_COLUMNS)
    ]
    backends: dict[str, list[dict[str, Any]]] = {}
    for number, r in _rows(book["backends"], BACKEND_COLUMNS):
        where = f"backends row {number}"
        if not r["source_type"]:
            raise EvidenceSourceError(f"{where}: choose a source_type")
        if not r["system"]:
            raise EvidenceSourceError(f"{where}: system is empty (write 'unknown' if not known)")
        status = (r["status"] or "").lower()
        if status not in STATUSES:
            raise EvidenceSourceError(f"{where}: status must be one of {', '.join(STATUSES)}")
        backends.setdefault(r["source_type"], []).append(
            {"system": r["system"], "dataset": r["dataset"], "status": status, "owner": r["owner"], "notes": r["notes"]}
        )
    proposals = []
    for number, r in _rows(book["proposals"], PROPOSAL_COLUMNS):
        if (r["kind"] or "").lower() not in PROPOSAL_KINDS:
            raise EvidenceSourceError(f"proposals row {number}: kind must be one of {', '.join(PROPOSAL_KINDS)}")
        if not r["what_and_why"]:
            raise EvidenceSourceError(f"proposals row {number}: what_and_why is empty")
        proposals.append({c: r[c] for c in PROPOSAL_COLUMNS} | {"kind": r["kind"].lower()})
    changes = [{c: r[c] for c in CHANGE_COLUMNS} for _, r in _rows(book["changes"], CHANGE_COLUMNS)]
    return Workbook(about["version"], types, backends, proposals, changes)


def _locked_view(taxonomy: EvidenceSourceTaxonomy) -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "description": t.description,
            "examples": t.examples,
            "deprecated": t.deprecated,
            "replaced_by": t.replaced_by,
        }
        for t in taxonomy.source_types
    ]


@dataclass
class ImportReport:
    version: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    new_proposals: int = 0
    new_changes: int = 0

    def lines(self) -> list[str]:
        out = [
            f"{self.version}: backends {len(self.added)} added, {len(self.removed)} removed, {len(self.changed)} changed"
        ]
        out += (
            [f"  + {x}" for x in self.added] + [f"  - {x}" for x in self.removed] + [f"  ~ {x}" for x in self.changed]
        )
        out.append(f"  {self.new_proposals} new proposals, {self.new_changes} new change-log entries")
        return out


def _describe(source_type: str, b: EvidenceBackend) -> str:
    return f"{source_type}: {b.system}{' ' + b.dataset if b.dataset else ''} [{b.status.value}]"


def import_workbook(path: Path, directory: Path = DEFAULT_REGISTRY_DIR) -> ImportReport:
    book = read_workbook(path)
    current = load_taxonomy(book.version, directory)
    if book.source_types != _locked_view(current):
        raise EvidenceSourceError(
            "the source_types sheet differs from the registry; it is maintained by the technical team. "
            "Put wording changes or new source types on the proposals sheet instead"
        )
    unknown = sorted(set(book.backends) - {t.id for t in current.source_types})
    if unknown:
        raise EvidenceSourceError(f"backends rows name unknown source types {unknown}")
    missing = [t.id for t in current.source_types if t.id not in book.backends]
    if missing:
        raise EvidenceSourceError(f"every source type needs a backends row (status 'none' if unknown): {missing}")
    try:
        updated = EvidenceSourceTaxonomy.model_validate(
            current.model_dump(mode="json")
            | {
                "source_types": [
                    t.model_dump(mode="json") | {"backends": book.backends[t.id]} for t in current.source_types
                ]
            }
        )
    except ValidationError as exc:
        raise EvidenceSourceError(f"{path}: {exc}") from None
    duplicates = _duplicate_backends(updated)
    if duplicates:
        raise EvidenceSourceError(f"duplicate backends rows: {duplicates}")

    report = ImportReport(book.version)
    for old, new in zip(current.source_types, updated.source_types, strict=True):
        before = {_backend_key(b): b for b in old.backends}
        after = {_backend_key(b): b for b in new.backends}
        report.added += [_describe(new.id, after[k]) for k in after.keys() - before.keys()]
        report.removed += [_describe(old.id, before[k]) for k in before.keys() - after.keys()]
        report.changed += [_describe(new.id, after[k]) for k in after.keys() & before.keys() if after[k] != before[k]]
    save(updated, directory)
    report.new_proposals, report.new_changes = _append_requests(book, directory)
    return report


def _append_requests(book: Workbook, directory: Path) -> tuple[int, int]:
    path = requests_path_for(book.version, directory)
    log = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    log = log or {"version": book.version, "proposals": [], "changes": []}

    def key(entry: dict, columns: tuple[str, ...]) -> tuple:
        return tuple((entry.get(c) or "") for c in columns if c != "status")

    added = []
    for name, columns, rows in (
        ("proposals", PROPOSAL_COLUMNS, book.proposals),
        ("changes", CHANGE_COLUMNS, book.changes),
    ):
        known = {key(e, columns) for e in log[name]}
        fresh = [
            r | ({"status": r.get("status") or "open"} if name == "proposals" else {})
            for r in rows
            if key(r, columns) not in known
        ]
        log[name] += fresh
        added.append(len(fresh))
    if any(added):
        path.write_text(
            "# Proposals and change log from the evidence sources workbook. The technical team sets each\n"
            "# proposal's status (open / accepted / rejected) and, if useful, a response.\n"
            + yaml.safe_dump(log, sort_keys=False, allow_unicode=True, width=120),
            encoding="utf-8",
        )
    return added[0], added[1]


def to_workbook(taxonomy: EvidenceSourceTaxonomy, path: Path, directory: Path = DEFAULT_REGISTRY_DIR) -> None:
    from openpyxl import Workbook as Book
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    log_path = requests_path_for(taxonomy.version, directory)
    log = (yaml.safe_load(log_path.read_text(encoding="utf-8")) if log_path.is_file() else None) or {}

    book = Book()
    read_me = book.active
    read_me.title = "read_me"
    for row in (
        ("version", taxonomy.version),
        ("guide", "lld/guides/evidence-sources.md"),
        ("backends", "Fill in where each kind of evidence lives. Choose source_type and status from the dropdowns."),
        ("proposals", "Suggest new kinds of evidence, better wording, or retiring one. Plain words; no customer data."),
        ("changes", "Add a line for every change you make: date, who, what, why."),
        ("source_types", "Reference only (locked). Maintained by the technical team."),
    ):
        read_me.append(row)

    types = book.create_sheet("source_types")
    types.append(list(TYPE_COLUMNS))
    for t in taxonomy.source_types:
        types.append([t.id, t.description, "\n".join(t.examples), "Y" if t.deprecated else "N", t.replaced_by or ""])
    types.protection.sheet = True  # no password: guards against accidental edits, import checks the rest

    ids = f"source_types!$A$2:$A${len(taxonomy.source_types) + 1}"
    backends = book.create_sheet("backends")
    backends.append(list(BACKEND_COLUMNS))
    for t in taxonomy.source_types:
        for b in t.backends:
            backends.append([t.id, b.system, b.dataset or "", b.status.value, b.owner or "", b.notes or ""])
    for validation, column in (
        (DataValidation(type="list", formula1=f"={ids}", allow_blank=False), "A"),
        (DataValidation(type="list", formula1=f'"{",".join(STATUSES)}"', allow_blank=False), "D"),
    ):
        validation.error = "Choose a value from the list"
        validation.showErrorMessage = True
        backends.add_data_validation(validation)
        validation.add(f"{column}2:{column}{_ROWS}")
    backends.conditional_formatting.add(
        f"A2:F{_ROWS}",
        FormulaRule(
            formula=['AND($A2<>"",COUNTIFS($A$2:$A$500,$A2,$B$2:$B$500,$B2,$C$2:$C$500,$C2)>1)'],
            fill=PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
        ),
    )

    proposals = book.create_sheet("proposals")
    proposals.append(list(PROPOSAL_COLUMNS))
    for p in log.get("proposals", []):
        proposals.append([p.get(c) or "" for c in PROPOSAL_COLUMNS])
    for validation, column in (
        (DataValidation(type="list", formula1=f'"{",".join(PROPOSAL_KINDS)}"', allow_blank=False), "A"),
        (DataValidation(type="list", formula1=f"={ids}", allow_blank=True), "B"),
    ):
        proposals.add_data_validation(validation)
        validation.add(f"{column}2:{column}{_ROWS}")

    changes = book.create_sheet("changes")
    changes.append(list(CHANGE_COLUMNS))
    for c in log.get("changes", []):
        changes.append([c.get(k) or "" for k in CHANGE_COLUMNS])

    widths = {
        "read_me": (14, 100),
        "source_types": (22, 60, 50, 12, 16),
        "backends": (22, 22, 46, 12, 18, 50),
        "proposals": (18, 22, 60, 50, 16, 12, 10),
        "changes": (12, 16, 60, 60),
    }
    for sheet in book.worksheets:
        for i, width in enumerate(widths[sheet.title]):
            sheet.column_dimensions[chr(ord("A") + i)].width = width
        if sheet.title != "read_me":
            sheet.freeze_panes = "A2"
            for cell in sheet[1]:
                cell.font = Font(bold=True)
        for row in sheet.iter_rows(min_row=1 if sheet.title == "read_me" else 2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    book.save(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cfr_v2.registry.evidence_sources", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_DIR, help="registry directory")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="write a version (with its proposals and change log) to a workbook")
    exp.add_argument("version")
    exp.add_argument("workbook", type=Path)
    imp = sub.add_parser("import", help="apply a workbook's backends, proposals and change log to its version")
    imp.add_argument("workbook", type=Path)
    sub.add_parser("check", help="validate and lint every version in the registry")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            to_workbook(load_taxonomy(args.version, args.registry), args.workbook, args.registry)
            print(f"wrote {args.workbook}")
        elif args.command == "import":
            print("\n".join(import_workbook(args.workbook, args.registry).lines()))
        else:
            failed = False
            for version in versions(args.registry):
                taxonomy = load_taxonomy(version, args.registry)
                problems = lint(taxonomy) + [f"duplicate backend {d}" for d in _duplicate_backends(taxonomy)]
                gaps = [t.id for t in taxonomy.active() if all(b.status.value == "none" for b in t.backends)]
                print(f"{version}: {len(taxonomy.active())} active source types; no source yet for {gaps or 'none'}")
                for problem in problems:
                    print(f"  problem: {problem}")
                failed = failed or bool(problems)
            if failed:
                return 1
    except EvidenceSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
