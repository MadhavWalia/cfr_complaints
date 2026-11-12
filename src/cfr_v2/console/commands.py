"""The command catalogue (LDR-0023 decision 2): every console action is one pipeline command.

A ``Command`` declares its form fields, what it will do (``effect``, shown before it starts),
and how the submitted values become the exact arguments the terminal would use. Nothing here
holds pipeline logic: the command does the work, and its log and files are the job's.

Files a command writes go to the job's ``outputs/``; uploads are saved to its ``inputs/``.
Paths on this machine (a PDF folder, the LightRAG store) are typed, relative to the repository,
because they are too large to upload. An earlier job's outputs can feed a later command (the
C02 trial's review files into the router trial).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import yaml

from cfr_v2.procedures import stores
from cfr_v2.registry.manifests import ReleaseRegistry

from . import formats

FieldKind = Literal["text", "int", "float", "date", "check", "choice", "file", "files", "path", "lines", "job", "ref"]
OTHER = "__other__"  # a ref field's "Another ref…" choice: the value is then typed in <name>_other


class FormError(ValueError):
    """A submitted value the command can't take; the message names the field and the fix."""


@dataclass(frozen=True)
class Context:
    """What a form needs from the console: where paths are relative to, the environment for
    defaults, and earlier jobs to choose from."""

    root: Path
    env: Mapping[str, str]
    done_jobs: Callable[[str], Sequence[Any]] = lambda command: ()  # finished, successful, newest first
    outputs: Callable[[str], Path] = lambda job_id: Path(job_id)  # a job's outputs/ folder


Default = Any  # a value, or a callable taking the Context


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    default: Default = None
    help: str | None = None
    choices: tuple[tuple[str, str], ...] | Callable[[Context], Sequence[tuple[str, str]]] = ()  # (value, label)
    minimum: float | None = None
    maximum: float | None = None
    accept: str | None = None  # file types, e.g. ".xlsx,.csv"
    must: Literal["dir", "file"] | None = None  # a path field: what must exist there
    source: str | None = None  # a job field: the command whose outputs it takes
    either: bool = False  # one of a pair the command needs exactly one of: not marked optional

    def initial(self, ctx: Context) -> Any:
        return self.default(ctx) if callable(self.default) else self.default

    def options(self, ctx: Context) -> list[tuple[str, str]]:
        if self.kind == "job":
            return [(j.id, f"{j.title} ({formats.when(j.created_at)})") for j in ctx.done_jobs(self.source or "")]
        return list(self.choices(ctx) if callable(self.choices) else self.choices)

    def read(self, form: Mapping[str, Any]) -> Any:
        if self.kind == "ref" and form.get(self.name) == OTHER:
            return form.get(f"{self.name}_other")
        if self.kind == "files":
            getlist = getattr(form, "getlist", None)
            return getlist(self.name) if getlist else form.get(self.name) or []
        return form.get(self.name)

    def parse(self, raw: Any, ctx: Context) -> Any:
        """The submitted value, checked and converted; the default (or None) when left empty."""
        if self.kind == "check":
            return raw in ("on", "true", "1", True)
        if self.kind == "file":
            present = raw is not None and getattr(raw, "filename", "")
            if not present and self.required:
                raise FormError(f"{self.label}: choose a file.")
            return raw if present else None
        if self.kind == "files":
            uploads = [u for u in (raw if isinstance(raw, list | tuple) else [raw]) if getattr(u, "filename", "")]
            if not uploads and self.required:
                raise FormError(f"{self.label}: choose at least one file.")
            return uploads
        text = "" if raw is None else str(raw).strip()
        if not text:
            text = "" if (initial := self.initial(ctx)) is None else str(initial)
        if not text:
            if self.required:
                raise FormError(f"{self.label}: required.")
            return () if self.kind == "lines" else None
        match self.kind:
            case "int" | "float":
                return self._number(text)
            case "date":
                try:
                    return date.fromisoformat(text)
                except ValueError:
                    raise FormError(f"{self.label}: a date as YYYY-MM-DD.") from None
            case "lines":
                return tuple(dict.fromkeys(v for v in re.split(r"[\s,;]+", text) if v))
            case "path":
                return self._path(text, ctx)
            case "job":
                if text not in {value for value, _ in self.options(ctx)}:
                    raise FormError(f"{self.label}: choose one of the finished jobs.")
                return ctx.outputs(text)
            case "choice":
                if text not in {value for value, _ in self.options(ctx)}:
                    raise FormError(f"{self.label}: choose one of the options.")
            case "ref":
                if not re.fullmatch(r"sha256:[A-Za-z0-9]+", text):
                    raise FormError(f"{self.label}: a ref such as sha256:…, or one of the choices.")
        return text

    def _number(self, text: str) -> float | int:
        try:
            value: float | int = int(text) if self.kind == "int" else float(text)
        except ValueError:
            raise FormError(
                f"{self.label}: {'a whole number' if self.kind == 'int' else 'a number'}, please."
            ) from None
        if self.minimum is not None and value < self.minimum:
            raise FormError(f"{self.label}: at least {self.minimum:g}.")
        if self.maximum is not None and value > self.maximum:
            raise FormError(f"{self.label}: at most {self.maximum:g}.")
        return value

    def _path(self, text: str, ctx: Context) -> Path:
        path = (ctx.root / Path(text).expanduser()).resolve()
        if self.must == "dir" and not path.is_dir():
            raise FormError(f"{self.label}: no folder at {text}.")
        if self.must == "file" and not path.is_file():
            raise FormError(f"{self.label}: no file at {text}.")
        return path


Values = Mapping[str, Any]


# What a command does, as the same few words everywhere (LDR-0025 step 4).
TAGS = {
    "changes nothing": "Reads only; writes nothing but its own job folder",
    "writes the database": "Writes to the state database or the artifact store",
    "changes repository files": "Changes files under registry/: review with git diff, then commit",
    "calls gpt-5": "Spends model calls on the gateway",
    "long-running": "Runs for hours, or until stopped",
    "keep output local": "Its files hold case or procedure text: keep them on this machine",
}


@dataclass(frozen=True)
class Estimate:
    """Model calls, before starting: ``text`` with ``{n}`` and ``{calls}``; with ``field``, the
    page recomputes it as that field changes (``calls = n × per``)."""

    text: str
    n: int
    per: float
    field: str | None = None

    @property
    def calls(self) -> int:
        return round(self.n * self.per)

    def render(self) -> str:
        return self.text.format(n=f"{self.n:,}", calls=f"{self.calls:,}")


@dataclass(frozen=True)
class Next:
    """A step that usually follows a finished job: another command's form, filled in from the job
    (``values``), or a page (``page``)."""

    label: str
    command: str | None = None
    values: Callable[[Any], Mapping[str, str]] = lambda job: {}
    page: Callable[[Any], str] | None = None
    when: Callable[[Any], bool] = field(default=lambda job: job.status.value == "done")

    def href(self, job: Any) -> str:
        if self.page is not None:
            return self.page(job)
        target = BY_ID[self.command or ""]
        query = urlencode({"fill": target.id, "from": job.id, **self.values(job)})
        return f"/{target.area}?{query}#{target.id}"


@dataclass(frozen=True)
class Command:
    id: str  # "<area>.<name>"
    area: str  # the rail area it lives in
    title: str
    effect: str  # what happens, before it does: model calls, writes, scale, what to keep local
    module: str  # python -m <module>
    args: Callable[[Values, Path], Sequence[str]]  # values and the job folder → arguments
    fields: tuple[Field, ...] = ()
    job_title: Callable[[Values], str] | None = None
    summarize: Callable[[Path, int], str | None] | None = None
    check: Callable[[Values, Context], None] | None = None  # rules across fields; raises FormError
    long_running: bool = False  # runs until stopped (a worker)
    action: str = "Start"  # the button: names what it does
    next: tuple[Next, ...] = ()  # what usually follows a finished job of this command
    tags: tuple[str, ...] = ()  # from TAGS
    estimate: Callable[[Context], Estimate | None] | None = None  # model calls, when they can be counted

    def next_links(self, job: Any) -> list[tuple[str, str]]:
        """``(label, href)`` for each next step that applies to ``job``."""
        return [(n.label, n.href(job)) for n in self.next if n.when(job)]

    def parse(self, form: Mapping[str, Any], ctx: Context) -> dict[str, Any]:
        values, problems = {}, []
        for f in self.fields:
            try:
                values[f.name] = f.parse(f.read(form), ctx)
            except FormError as exc:
                problems.append(str(exc))
        if not problems and self.check is not None:
            try:
                self.check(values, ctx)
            except FormError as exc:
                problems.append(str(exc))
        if problems:
            raise FormError(" ".join(problems))
        return values

    def argv(self, values: Values, folder: Path) -> list[str]:
        return [sys.executable, "-m", self.module, *self.args(values, folder)]

    def describe(self, values: Values) -> str:
        return self.job_title(values) if self.job_title else self.title


def save_upload(upload: Any, folder: Path) -> Path:
    """An uploaded file into the job's ``inputs/``, under its own base name only."""
    name = Path(getattr(upload, "filename", "") or "upload").name or "upload"
    path = folder / "inputs" / name
    with path.open("wb") as out:
        shutil.copyfileobj(upload.file, out)
    return path


# --- summaries: one line for the journal, from what the command printed or wrote ---------------


def _log(folder: Path) -> str:
    return (folder / "log.txt").read_text(encoding="utf-8", errors="replace")


def _copy_log_to(name: str) -> Callable[[Path, int], str | None]:
    def summarize(folder: Path, code: int) -> str | None:
        if code == 0:
            shutil.copyfile(folder / "log.txt", folder / "outputs" / name)
            return name
        return None

    return summarize


def _printed(pattern: str) -> Callable[[Path, int], str | None]:
    """The last match of ``pattern``'s first group in the log (multiline)."""

    def summarize(folder: Path, code: int) -> str | None:
        found = re.findall(pattern, _log(folder), flags=re.MULTILINE)
        return found[-1].strip() if found else None

    return summarize


def _wrote(subdir: str, noun: str) -> Callable[[Path, int], str | None]:
    def summarize(folder: Path, code: int) -> str | None:
        base = folder / "outputs" / subdir
        count = sum(1 for p in base.rglob("*") if p.is_file()) if base.is_dir() else 0
        return f"{count} {noun}{'' if count == 1 else 's'}" if count else None

    return summarize


def _intake_counts(folder: Path, code: int) -> str | None:
    """The summary JSON intake prints last: ``3 accepted, 0 duplicate``."""
    found = re.findall(r"\{[^{}]*\"manifest_ref\"[^{}]*\}", _log(folder), flags=re.DOTALL)
    if not found:
        return None
    counts = json.loads(found[-1])
    parts = [f"{counts.get('ACCEPTED', 0)} accepted", f"{counts.get('DUPLICATE', 0)} duplicate"]
    for key in ("REJECTED", "FAILED"):
        if counts.get(key):
            parts.append(f"{counts[key]} {key.lower()}")
    if "not_returned" in counts:
        parts.append(f"{counts['not_returned']} not returned")
    return ", ".join(parts)


# --- defaults and choices read from this machine -------------------------------------------------

VERTICALS_DOCUMENT = "config/verticals/knowledge_document.txt"
STORE_HELP = "Empty: the snapshot's own folder, lightrag_store_ and the start of its hash, where the worker looks."


def _release_snapshot(ctx: Context) -> str | None:
    """The procedure snapshot the current release (``CFR_RELEASE_ID``) pins: the one the worker searches."""
    try:
        manifest = ReleaseRegistry(ctx.root / "registry").load(ctx.env.get("CFR_RELEASE_ID", ""))
    except (OSError, ValueError, yaml.YAMLError):  # no such release, or a broken file: no default
        return None
    return manifest.procedure_snapshot


def _latest_snapshot(ctx: Context) -> str | None:
    """For a build: the newest snapshot made here, else the release's."""
    for job in ctx.done_jobs("procedures.snapshot"):
        if job.summary and job.summary.startswith("sha256:"):
            return job.summary
    return _release_snapshot(ctx)


def known_snapshots(ctx: Context) -> list[tuple[str, str]]:
    """Snapshots the console knows: from snapshot jobs, release pins and stores on disk, each
    labelled with where it was seen and whether its LightRAG store is built here."""
    seen: dict[str, list[str]] = {}
    for job in ctx.done_jobs("procedures.snapshot"):
        if job.summary and job.summary.startswith("sha256:"):
            seen.setdefault(job.summary, []).append(f"taken {formats.when(job.created_at)}")
    for path in sorted((ctx.root / "registry" / "releases").glob("*.yaml"), reverse=True):
        try:
            pinned = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("procedure_snapshot")
        except (OSError, yaml.YAMLError, AttributeError):
            continue
        if isinstance(pinned, str) and pinned.startswith("sha256:"):
            seen.setdefault(pinned, []).append(f"pinned by {path.stem}")
    for folder in sorted(ctx.root.glob(f"{stores.STORE_PREFIX}*")):
        if (ref := stores.built_from(folder)) is not None:
            seen.setdefault(ref, [])
    choices = []
    for ref, where in seen.items():
        try:
            built = stores.built_from(stores.store_dir(ref, root=ctx.root, env=ctx.env)) == ref
        except ValueError:
            continue
        label = " · ".join([f"{ref.removeprefix('sha256:')[:12]}…", *where, "store built" if built else "no store yet"])
        choices.append((ref, label))
    return choices


def known_verticals(ctx: Context) -> list[tuple[str, str]]:
    """Knowledge documents the console knows: published from here, or pinned by a release."""
    seen: dict[str, list[str]] = {}
    for job in ctx.done_jobs("routing.publish"):
        if job.summary and job.summary.startswith("sha256:"):
            seen.setdefault(job.summary, []).append(f"published {formats.when(job.created_at)}")
    for path in sorted((ctx.root / "registry" / "releases").glob("*.yaml"), reverse=True):
        try:
            pinned = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("routing_verticals")
        except (OSError, yaml.YAMLError, AttributeError):
            continue
        if isinstance(pinned, str) and pinned.startswith("sha256:"):
            seen.setdefault(pinned, []).append(f"pinned by {path.stem}")
    return [(ref, " · ".join([f"{ref.removeprefix('sha256:')[:12]}…", *where])) for ref, where in seen.items()]


def _build_estimate(ctx: Context) -> Estimate | None:
    """The newest snapshot's sections, from its job's summary lines, at about three calls each."""
    for job in ctx.done_jobs("procedures.snapshot"):
        try:
            log = (ctx.outputs(job.id).parent / "log.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if match := re.search(r"^current: [\d,]+ pages; ([\d,]+) sections", log, re.MULTILINE):
            sections = int(match.group(1).replace(",", ""))
            return Estimate(
                "About {calls} gpt-5 calls for the newest snapshot: {n} sections, about 3 each.", sections, 3
            )
    return None


def _trial_estimate(ctx: Context) -> Estimate:
    return Estimate("Up to {calls} gpt-5 calls: {n} cases, Pass 1, Pass 2 and C03 each.", 5, 3, field="sample")


def _backfill_estimate(ctx: Context) -> Estimate | None:
    """From the newest count: one search and one router call per claim."""
    for job in ctx.done_jobs("routing.backfill"):
        if job.summary and (match := re.match(r"([\d,]+) runs, ([\d,]+) claims", job.summary)):
            claims = int(match.group(2).replace(",", ""))
            return Estimate(
                "About {calls} gpt-5 calls for the last count's {n} claims (a search and a router call each).",
                claims,
                2,
            )
    return None


def _ref_after(job: Any, word: str) -> str:
    """The argument after ``word`` in a job's command line (e.g. the snapshot after lightrag-build)."""
    argv = list(job.argv)
    return argv[argv.index(word) + 1] if word in argv[:-1] else ""


def _store_field(help: str) -> Field:
    return Field("workdir", "LightRAG store", "path", help=help)


def _store_args(values: Values) -> list[str]:
    return _opt("--workdir", values["workdir"])


def _store_check(build: bool) -> Callable[[Values, Context], None]:
    """The store a command will use must hold its snapshot; a build may also start a new one."""

    def check(values: Values, ctx: Context) -> None:
        ref = values["procedures"]
        try:
            folder = values["workdir"] or stores.store_dir(ref, root=ctx.root, env=ctx.env)
        except ValueError:
            raise FormError("Snapshot: a ref such as sha256:…") from None
        built = stores.built_from(folder)
        if built == ref or (build and built is None):
            return
        name = folder.name
        if built is None:
            problem = f"there is no store for it at {name}. Build it first, under Procedures"
        else:
            problem = f"{name} was built from {built[:19]}…, and a store holds one snapshot"
        if values["workdir"]:
            fix = "Leave the store empty to use the snapshot's own"
        elif ctx.env.get(stores.WORKDIR_ENV):
            fix = f"{stores.WORKDIR_ENV} in .env chooses that folder: remove it to use {stores.store_name(ref)}"
        else:
            fix = ""
        raise FormError(f"LightRAG store: {problem}." + (f" {fix}." if fix else ""))

    return check


def _registry(kind: str) -> Callable[[Context], list[tuple[str, str]]]:
    def choices(ctx: Context) -> list[tuple[str, str]]:
        return [(p.stem, p.stem) for p in sorted((ctx.root / "registry" / kind).glob("*.yaml"))]

    return choices


def _one_of(*names: str, message: str) -> Callable[[Values, Context], None]:
    def check(values: Values, ctx: Context) -> None:
        if sum(bool(values.get(n)) for n in names) != 1:
            raise FormError(message)

    return check


def _all(*checks: Callable[[Values, Context], None]) -> Callable[[Values, Context], None]:
    def check(values: Values, ctx: Context) -> None:
        for one in checks:
            one(values, ctx)

    return check


def _window(values: Values, ctx: Context) -> None:
    if values["end_date"] < values["start_date"]:
        raise FormError("To: on or after From.")


def _out(folder: Path, name: str) -> str:
    return str(folder / "outputs" / name)


def _flag(on: Any, *args: str) -> list[str]:
    return list(args) if on else []


def _opt(name: str, value: Any) -> list[str]:
    return [] if value in (None, "", ()) else [name, str(value)]


def _resume_backfill(v: Values, folder: Path) -> list[str]:
    out = folder / "outputs" / "backfill.jsonl"
    if v["resume"] is not None and (v["resume"] / "backfill.jsonl").is_file():
        shutil.copyfile(v["resume"] / "backfill.jsonl", out)  # runs already done there are skipped
    return ["--out", str(out)]


ALLOW_INCOMPATIBLE = Field(
    "allow_incompatible",
    "Allow a release that doesn't match this code",
    "check",
    help="Only when you know why the stage versions differ.",
)
KEEP_LOCAL = "Keep them on this machine and delete this job once reviewed."

CATALOGUE: tuple[Command, ...] = (
    # --- Intake ---------------------------------------------------------------------------------
    Command(
        id="intake.window",
        area="intake",
        title="A date window",
        effect="Queries BigQuery for the complaints raised in the window and admits them under the current "
        "release. Nothing calls a model until a worker runs them.",
        module="cfr_v2.intake",
        fields=(
            Field("start_date", "From", "date", required=True),
            Field("end_date", "To", "date", required=True, help="Inclusive."),
            ALLOW_INCOMPATIBLE,
        ),
        args=lambda v, folder: [
            "--start-date",
            v["start_date"].isoformat(),
            "--end-date",
            v["end_date"].isoformat(),
            *_flag(v["allow_incompatible"], "--allow-incompatible"),
        ],
        check=_window,
        job_title=lambda v: f"Intake · {v['start_date']:%-d %b} to {v['end_date']:%-d %b %Y}",
        summarize=_intake_counts,
        action="Admit the window",
        next=(Next("Start a worker", page=lambda job: "/system#system.worker"),),
        tags=("writes the database",),
    ),
    Command(
        id="intake.case_list",
        area="intake",
        title="A list of case numbers",
        effect="Admits the cases listed in a spreadsheet column, with the same BigQuery query as a date window. "
        "The case numbers it didn't return are written to missing.txt.",
        module="cfr_v2.intake",
        fields=(
            Field("cases", "Spreadsheet", "file", required=True, accept=".xlsx,.csv"),
            Field("column", "Column", required=True, default="case_no"),
            Field("sheet", "Worksheet", help="Otherwise the first."),
            ALLOW_INCOMPATIBLE,
        ),
        args=lambda v, folder: [
            "--case-nos-from",
            str(v["cases"]),
            "--column",
            v["column"],
            *_opt("--sheet", v["sheet"]),
            "--missing-out",
            _out(folder, "missing.txt"),
            *_flag(v["allow_incompatible"], "--allow-incompatible"),
        ],
        job_title=lambda v: f"Intake · cases in {Path(getattr(v['cases'], 'filename', '') or v['cases']).name}",
        summarize=_intake_counts,
        action="Admit the cases",
        next=(Next("Start a worker", page=lambda job: "/system#system.worker"),),
        tags=("writes the database",),
    ),
    Command(
        id="intake.synthetic",
        area="intake",
        title="Synthetic cases",
        effect="Admits synthetic cases (no customer data) under the current release. Nothing calls a model until "
        "a worker runs them. A seed used before gives only duplicates.",
        module="cfr_v2.intake",
        fields=(
            Field("count", "Cases", "int", required=True, default=3, minimum=1, maximum=500),
            Field(
                "seed",
                "Seed",
                "int",
                required=True,
                default=1,
                minimum=0,
                maximum=99,
                help="Case numbers come from the seed: SYN<seed><n>.",
            ),
        ),
        args=lambda v, folder: ["--synthetic", str(v["count"]), "--seed", str(v["seed"])],
        job_title=lambda v: f"Intake · {v['count']} synthetic cases, seed {v['seed']}",
        summarize=_intake_counts,
        action="Admit cases",
        next=(Next("Start a worker", page=lambda job: "/system#system.worker"),),
        tags=("writes the database",),
    ),
    Command(
        id="intake.sql",
        area="intake",
        title="Show the intake query",
        effect="Writes the BigQuery query intake would run, to read or check its cost. Runs nothing.",
        module="cfr_v2.intake",
        fields=(Field("by_case_no", "For a list of case numbers", "check", help="Otherwise, for a date window."),),
        args=lambda v, folder: ["--print-sql", *_flag(v["by_case_no"], "--case-nos-from", "cases.xlsx")],
        job_title=lambda v: "Intake query" + (" · by case number" if v["by_case_no"] else " · date window"),
        summarize=_copy_log_to("intake.sql"),
        action="Write the query",
        tags=("changes nothing",),
    ),
    # --- Routing --------------------------------------------------------------------------------
    Command(
        id="routing.trial",
        area="routing",
        title="Router trial",
        effect="Routes the claims in a C02 trial's review files, outside the workflow: per claim, one LightRAG "
        "search (one gpt-5 call) and one router call. Writes nothing to the database or the artifact store. "
        f"Its review files and routing.xlsx hold claim and procedure text. {KEEP_LOCAL}",
        module="cfr_v2.routing",
        fields=(
            Field("claims_job", "Claims from a C02 trial", "job", source="evaluation.trial", either=True),
            Field("claims_path", "Or a folder of review files", "path", must="dir", either=True),
            Field("procedures", "Snapshot", "ref", required=True, default=_release_snapshot, choices=known_snapshots),
            _store_field(STORE_HELP),
            Field("verticals", "Verticals document", "path", required=True, default=VERTICALS_DOCUMENT, must="file"),
            Field("cases", "Cases", "int", minimum=1, help="Only the first N cases; otherwise all."),
            Field("concurrency", "Claims at once", "int", required=True, default=4, minimum=1, maximum=32),
        ),
        args=lambda v, folder: [
            v["procedures"],
            *_store_args(v),
            "--verticals",
            str(v["verticals"]),
            "--claims-from",
            str((v["claims_job"] / "c02_trial") if v["claims_job"] else v["claims_path"]),
            *_opt("--cases", v["cases"]),
            "--concurrency",
            str(v["concurrency"]),
            "--out",
            _out(folder, "router_trial"),
        ],
        check=_all(
            _one_of("claims_job", "claims_path", message="Claims: choose a C02 trial or type a folder, not both."),
            _store_check(build=False),
        ),
        job_title=lambda v: "Router trial" + (f" · {v['cases']} cases" if v["cases"] else ""),
        summarize=_wrote("router_trial", "file"),
        action="Route the claims",
        tags=("calls gpt-5", "keep output local"),
    ),
    Command(
        id="routing.publish",
        area="routing",
        title="Publish the verticals",
        effect="Stores the knowledge document as an INTERNAL artifact and prints its ref, for a release "
        "manifest's routing_verticals. Prints counts and the ref only.",
        module="cfr_v2.routing",
        fields=(Field("path", "Verticals document", "path", required=True, default=VERTICALS_DOCUMENT, must="file"),),
        args=lambda v, folder: ["publish-verticals", str(v["path"])],
        job_title=lambda v: "Publish the verticals",
        summarize=_printed(r"stored as (\S+)"),
        action="Publish",
        tags=("writes the database",),
    ),
    Command(
        id="routing.backfill",
        area="routing",
        title="Backfill",
        effect="Routes the stored claim sets of runs that failed under an older release, with the routing pins "
        "of the current one: one search and one router call per claim. Count first: it makes no model calls.",
        module="cfr_v2.routing",
        fields=(
            Field("from_release", "Failed under", "choice", required=True, choices=_registry("releases")),
            Field("count_only", "Count only", "check", default=True, help="Runs and claims; no model calls."),
            Field("limit", "Runs", "int", minimum=1, help="Only the first N still to do; otherwise all."),
            Field("concurrency", "Runs at once", "int", required=True, default=4, minimum=1, maximum=32),
            _store_field("Empty: the store of the snapshot the current release pins, as the worker uses."),
            Field("resume", "Continue from", "job", source="routing.backfill", help="Skips the runs it routed."),
        ),
        args=lambda v, folder: [
            "backfill",
            "--from-release",
            v["from_release"],
            *_store_args(v),
            *_opt("--limit", v["limit"]),
            "--concurrency",
            str(v["concurrency"]),
            *_flag(v["count_only"], "--count-only"),
            *_resume_backfill(v, folder),
        ],
        job_title=lambda v: f"Backfill · {v['from_release']}" + (" · count" if v["count_only"] else ""),
        summarize=_printed(r"^(\d+ runs, \d+ claims)"),
        action="Start",
        next=(
            Next(
                "Route them",
                "routing.backfill",
                lambda job: {**(job.form or {}), "count_only": ""},  # the same runs, routed this time
                when=lambda job: job.status.value == "done" and "--count-only" in job.argv,
            ),
        ),
        tags=("calls gpt-5", "writes the database"),
        estimate=_backfill_estimate,
    ),
    # --- Procedures -----------------------------------------------------------------------------
    Command(
        id="procedures.inspect",
        area="procedures",
        title="Inspect PDFs",
        effect="Reports pages, text, tables and duplicates for a folder of procedure PDFs. Numbers and file names "
        "only, so the report can be shared. Stores nothing.",
        module="cfr_v2.procedures",
        fields=(Field("folder", "PDF folder", "path", required=True, must="dir"),),
        args=lambda v, folder: ["inspect", str(v["folder"]), "--out", _out(folder, "inspect.json")],
        job_title=lambda v: f"Inspect · {v['folder'].name}",
        summarize=_printed(r"^(\d+ of \d+ files read)"),
        action="Inspect",
        tags=("changes nothing",),
    ),
    Command(
        id="procedures.extract",
        area="procedures",
        title="Extraction trial",
        effect="Extracts and redacts the PDFs' sections without storing anything, and writes the redacted text "
        f"to read. That text is procedure content. {KEEP_LOCAL}",
        module="cfr_v2.procedures",
        fields=(Field("folder", "PDF folder", "path", required=True, must="dir"),),
        args=lambda v, folder: ["extract", str(v["folder"]), "--out", _out(folder, "extract")],
        job_title=lambda v: f"Extraction trial · {v['folder'].name}",
        summarize=_wrote("extract", "file"),
        action="Extract",
        tags=("changes nothing", "keep output local"),
    ),
    Command(
        id="procedures.snapshot",
        area="procedures",
        title="Snapshot",
        effect="Extracts, redacts and stores the PDFs and their sections as a new snapshot, and prints its ref. "
        "With a previous snapshot, unchanged PDFs aren't read again.",
        module="cfr_v2.procedures",
        fields=(
            Field("folder", "PDF folder", "path", required=True, must="dir"),
            Field(
                "previous",
                "Previous snapshot",
                "ref",
                choices=known_snapshots,
                help="Unchanged PDFs aren't read again.",
            ),
        ),
        args=lambda v, folder: ["snapshot", str(v["folder"]), *_opt("--previous", v["previous"])],
        job_title=lambda v: f"Snapshot · {v['folder'].name}",
        summarize=_printed(r"^snapshot: (\S+)"),
        action="Take the snapshot",
        next=(Next("Build its LightRAG store", "procedures.build", lambda job: {"procedures": job.summary or ""}),),
        tags=("writes the database",),
    ),
    Command(
        id="procedures.build",
        area="procedures",
        title="Build the LightRAG store",
        effect="Builds the graph and vectors from a snapshot's current procedures: about three gpt-5 calls per "
        "passage, hours for a full set. It resumes: passages already in the store are skipped.",
        module="cfr_v2.procedures",
        fields=(
            Field("procedures", "Snapshot", "ref", required=True, default=_latest_snapshot, choices=known_snapshots),
            _store_field(STORE_HELP + " Created if missing."),
            Field("concurrency", "Passages at once", "int", required=True, default=16, minimum=1, maximum=64),
        ),
        check=_store_check(build=True),
        args=lambda v, folder: [
            "lightrag-build",
            v["procedures"],
            *_store_args(v),
            "--concurrency",
            str(v["concurrency"]),
        ],
        job_title=lambda v: f"LightRAG build · {v['procedures'][:19]}…",
        summarize=_printed(r"^(\d+ of \d+ passages in the graph; \d+ failed)"),
        action="Build",
        next=(
            Next(
                "Make a release with this snapshot",
                page=lambda job: f"/releases?{urlencode({'snapshot': _ref_after(job, 'lightrag-build')})}#new-release",
            ),
            Next("Try a search", "procedures.search", lambda job: {"procedures": _ref_after(job, "lightrag-build")}),
        ),
        tags=("calls gpt-5", "long-running"),
        estimate=_build_estimate,
    ),
    Command(
        id="procedures.search",
        area="procedures",
        title="Try a search",
        effect="Searches the LightRAG store as the router would (one gpt-5 call for keywords). It prints section "
        f"titles, and passages if asked. {KEEP_LOCAL}",
        module="cfr_v2.procedures",
        fields=(
            Field("query", "Query", required=True),
            Field("procedures", "Snapshot", "ref", required=True, default=_release_snapshot, choices=known_snapshots),
            _store_field(STORE_HELP),
            Field("limit", "Results", "int", required=True, default=8, minimum=1, maximum=50),
            Field("text", "Show the passages", "check"),
        ),
        args=lambda v, folder: [
            "search",
            v["procedures"],
            v["query"],
            *_store_args(v),
            "--limit",
            str(v["limit"]),
            *_flag(v["text"], "--text"),
        ],
        job_title=lambda v: "Search",
        check=_store_check(build=False),
        action="Search",
        tags=("calls gpt-5", "keep output local"),
    ),
    # --- Evaluation -----------------------------------------------------------------------------
    Command(
        id="evaluation.trial",
        area="evaluation",
        title="C02 and C03 trial",
        effect="Runs Pass 1, Pass 2 and primary selection on admitted cases, outside the workflow: real gpt-5 "
        "calls, nothing written to the database or the artifact store. Prints counts, latency, tokens and a "
        f"cost projection. Its review files hold redacted transcript excerpts. {KEEP_LOCAL}",
        module="cfr_v2.understanding",
        fields=(
            Field("sample", "Random cases", "int", required=True, default=5, minimum=1, maximum=200),
            Field("case_run_ids", "Or these case runs", "lines", help="Ids, one per line; they replace the sample."),
            Field("pass1_only", "Pass 1 only", "check"),
            Field("workers", "Cases at once", "int", required=True, default=8, minimum=1, maximum=32),
            Field("total_cases", "Project the cost for", "int", required=True, default=10358, minimum=1),
        ),
        args=lambda v, folder: [
            *(
                [a for i in v["case_run_ids"] for a in ("--case-run-id", i)]
                if v["case_run_ids"]
                else ["--sample", str(v["sample"])]
            ),
            "--workers",
            str(v["workers"]),
            *_flag(v["pass1_only"], "--pass1-only"),
            "--total-cases",
            str(v["total_cases"]),
            "--out",
            _out(folder, "c02_trial"),
        ],
        job_title=lambda v: (
            f"C02 trial · {len(v['case_run_ids']) or v['sample']} cases" + (" · Pass 1" if v["pass1_only"] else "")
        ),
        summarize=_wrote("c02_trial", "review file"),
        action="Run the trial",
        next=(Next("Route these claims", "routing.trial", lambda job: {"claims_job": job.id}),),
        tags=("calls gpt-5", "keep output local"),
        estimate=_trial_estimate,
    ),
    Command(
        id="evaluation.gold_template",
        area="evaluation",
        title="Gold workbook",
        effect="Writes an empty gold workbook for labellers, for the cases listed. Runs nothing.",
        module="cfr_v2.understanding.score",
        fields=(
            Field("case_nos", "Case numbers", "lines", help="One per line.", either=True),
            Field("cases_file", "Or a text file of them", "file", accept=".txt,.csv", either=True),
        ),
        args=lambda v, folder: [
            "template",
            *[a for c in v["case_nos"] for a in ("--case-no", c)],
            *_opt("--cases-file", v["cases_file"]),
            _out(folder, "gold_template.xlsx"),
        ],
        check=_one_of("case_nos", "cases_file", message="Cases: type case numbers or choose a file, not both."),
        job_title=lambda v: "Gold workbook" + (f" · {len(v['case_nos'])} cases" if v["case_nos"] else ""),
        summarize=lambda folder, code: "gold_template.xlsx" if code == 0 else None,
        action="Write the workbook",
        next=(Next("Score Pass 1 against the workbooks", "evaluation.score"),),
        tags=("changes nothing",),
    ),
    Command(
        id="evaluation.score",
        area="evaluation",
        title="Score Pass 1",
        effect="Scores committed Pass 1 results against the labellers' workbooks: recall, precision, F1 and "
        "agreement. Needs RESTRICTED read access. Its report holds counts and case numbers only.",
        module="cfr_v2.understanding.score",
        fields=(
            Field(
                "gold",
                "Gold workbooks",
                "files",
                required=True,
                accept=".xlsx",
                help="The first wins for shared cases.",
            ),
            Field("run_missing", "Run Pass 1 for cases without a result", "check", help="Real gpt-5 calls."),
            Field("min_overlap", "Quote overlap to match", "float", required=True, default=0.3, minimum=0, maximum=1),
        ),
        args=lambda v, folder: [
            "run",
            *[str(p) for p in v["gold"]],
            *_flag(v["run_missing"], "--run-missing"),
            "--min-overlap",
            str(v["min_overlap"]),
            "--out",
            _out(folder, "pass1_score.json"),
        ],
        job_title=lambda v: f"Score Pass 1 · {len(v['gold'])} workbook{'' if len(v['gold']) == 1 else 's'}",
        summarize=lambda folder, code: (
            "pass1_score.json" if (folder / "outputs" / "pass1_score.json").is_file() else None
        ),
        action="Score",
        tags=("changes nothing",),
    ),
    Command(
        id="evaluation.sources_export",
        area="evaluation",
        title="Export evidence sources",
        effect="Writes an evidence-source version, with its proposals and change log, to a workbook to edit.",
        module="cfr_v2.registry.evidence_sources",
        fields=(Field("version", "Version", "choice", required=True, choices=_registry("evidence_sources")),),
        args=lambda v, folder: ["export", v["version"], _out(folder, f"{v['version']}.xlsx")],
        job_title=lambda v: f"Export · {v['version']}",
        summarize=lambda folder, code: "workbook" if code == 0 else None,
        action="Export",
        tags=("changes nothing",),
    ),
    Command(
        id="evaluation.sources_import",
        area="evaluation",
        title="Import evidence sources",
        effect="Applies an edited workbook's backends, proposals and change log to its version in registry/. "
        "That changes files in the repository: review them with git diff, then commit.",
        module="cfr_v2.registry.evidence_sources",
        fields=(Field("workbook", "Workbook", "file", required=True, accept=".xlsx"),),
        args=lambda v, folder: ["import", str(v["workbook"])],
        job_title=lambda v: f"Import · {Path(getattr(v['workbook'], 'filename', '') or v['workbook']).name}",
        action="Import",
        tags=("changes repository files",),
    ),
    Command(
        id="evaluation.sources_check",
        area="evaluation",
        title="Check evidence sources",
        effect="Validates and lints every evidence-source version in registry/. Changes nothing.",
        module="cfr_v2.registry.evidence_sources",
        args=lambda v, folder: ["check"],
        job_title=lambda v: "Check evidence sources",
        action="Check",
        tags=("changes nothing",),
    ),
    # --- System ---------------------------------------------------------------------------------
    Command(
        id="system.worker",
        area="system",
        title="Worker",
        effect="Runs queued case runs through the stages until stopped; every run calls the models. It refuses "
        "to start when more runs are queued than the cap, or when a queued release doesn't match this code. "
        "Stop lets it finish the cases it holds.",
        module="cfr_v2.runtime",
        fields=(
            Field(
                "max_queued",
                "At most this many queued",
                "int",
                required=True,
                default=10,
                minimum=0,
                help="Check the queue above first.",
            ),
            Field(
                "lanes",
                "Lanes",
                "choice",
                required=True,
                default="batch,realtime",
                choices=(("batch,realtime", "Batch and realtime"), ("batch", "Batch"), ("realtime", "Realtime")),
            ),
            Field("drain_timeout", "Seconds to finish on stop", "float", required=True, default=30, minimum=0),
            ALLOW_INCOMPATIBLE,
        ),
        args=lambda v, folder: [
            "--lanes",
            v["lanes"],
            "--max-queued",
            str(v["max_queued"]),
            "--drain-timeout",
            f"{v['drain_timeout']:g}",
            *_flag(v["allow_incompatible"], "--allow-incompatible"),
        ],
        job_title=lambda v: f"Worker · at most {v['max_queued']} queued",
        long_running=True,
        action="Start the worker",
        tags=("calls gpt-5", "writes the database", "long-running"),
    ),
    Command(
        id="system.smoke",
        area="system",
        title="Gateway checks",
        effect="Checks the model gateway with a few small calls: plain text, JSON output and trace headers, or "
        "the embedding model.",
        module="cfr_v2.llm.smoke",
        fields=(
            Field(
                "check",
                "Check",
                "choice",
                required=True,
                default="models",
                choices=(
                    ("models", "Chat models"),
                    ("embeddings", "Embedding model"),
                    ("embeddings-load", "Embedding model under a build's load"),
                ),
            ),
        ),
        args=lambda v, folder: {"models": [], "embeddings": ["--embeddings"]}.get(v["check"], ["--embeddings-load"]),
        job_title=lambda v: "Gateway check" + ("" if v["check"] == "models" else f" · {v['check']}"),
        summarize=_printed(r"^smoke: (\w+)"),
        action="Check",
        tags=("calls gpt-5",),
    ),
    Command(
        id="system.migrate",
        area="system",
        title="Database migrations",
        effect="Applies pending migrations to the state database. Intake and workers refuse to start until it "
        "is current.",
        module="cfr_v2.state.migrate",
        args=lambda v, folder: [],
        job_title=lambda v: "Migrations",
        summarize=_printed(r"^applied: (.*)"),
        action="Migrate",
        tags=("writes the database",),
    ),
    Command(
        id="system.tests",
        area="system",
        title="Tests",
        effect="Runs the test suite. Database tests use the local Postgres and RustFS, and skip when they're down.",
        module="pytest",
        fields=(
            Field(
                "suite",
                "Suite",
                "choice",
                required=True,
                default="offline",
                choices=(
                    ("offline", "Offline: unit and telemetry"),
                    ("db", "Database, without the slow ones"),
                    ("all", "Everything"),
                ),
            ),
        ),
        args=lambda v, folder: [
            "-q",
            *{"offline": ["tests/unit", "tests/telemetry"], "db": ["tests/db", "-m", "not slow"]}.get(v["suite"], []),
        ],
        job_title=lambda v: f"Tests · {v['suite']}",
        summarize=_printed(r"^=*\s*(\d+ (?:passed|failed|errors?)\b.*?)\s*=*$"),
        action="Run the tests",
    ),
)

BY_ID: dict[str, Command] = {c.id: c for c in CATALOGUE}


def _current_pin(key: str) -> Callable[[Context], str | None]:
    def pin(ctx: Context) -> str | None:
        try:
            return getattr(ReleaseRegistry(ctx.root / "registry").load(ctx.env.get("CFR_RELEASE_ID", "")), key)
        except (OSError, ValueError, yaml.YAMLError):
            return None

    return pin


# The release form on the Releases page (release_files.write_release).
NEW_RELEASE: tuple[Field, ...] = (
    Field(
        "base",
        "Based on",
        "choice",
        required=True,
        default=lambda ctx: ctx.env.get("CFR_RELEASE_ID"),
        choices=_registry("releases"),
        help="Its file is copied: stage versions and everything else stay as they are.",
    ),
    Field("release_id", "New release id", required=True, help="Lower-case letters, digits, dots and hyphens."),
    Field(
        "procedure_snapshot",
        "Procedure snapshot",
        "ref",
        required=True,
        default=_current_pin("procedure_snapshot"),
        choices=known_snapshots,
        help="The worker searches its LightRAG store, so build that first.",
    ),
    Field(
        "routing_verticals",
        "Knowledge document",
        "ref",
        required=True,
        default=_current_pin("routing_verticals"),
        choices=known_verticals,
        help="Publish a new one under Routing → Publish the verticals.",
    ),
)


def for_area(area: str) -> list[Command]:
    return [c for c in CATALOGUE if c.area == area]


__all__ = ["BY_ID", "CATALOGUE", "Command", "Context", "Field", "FormError", "for_area", "save_upload"]
