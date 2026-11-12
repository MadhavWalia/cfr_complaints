"""The command catalogue (LDR-0023 decision 2): every console action is one pipeline command.

A ``Command`` declares its form fields, what it will do (``effect``, shown before it starts),
and how the submitted values become the exact arguments the terminal would use. Nothing here
holds pipeline logic: the command does the work, and its log and files are the job's.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

FieldKind = Literal["text", "int", "date", "check", "choice", "file"]


class FormError(ValueError):
    """A submitted value the command can't take; the message names the field and the fix."""


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    default: Any = None
    help: str | None = None
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)
    minimum: int | None = None
    maximum: int | None = None
    accept: str | None = None  # file types, e.g. ".xlsx,.csv"

    def parse(self, raw: Any) -> Any:
        """The submitted value, checked and converted; None when left empty and optional."""
        if self.kind == "check":
            return raw in ("on", "true", "1", True)
        if self.kind == "file":
            present = raw is not None and getattr(raw, "filename", "")
            if not present and self.required:
                raise FormError(f"{self.label}: choose a file.")
            return raw if present else None
        text = "" if raw is None else str(raw).strip()
        if not text:
            if self.required:
                raise FormError(f"{self.label}: required.")
            return self.default
        if self.kind == "int":
            try:
                value = int(text)
            except ValueError:
                raise FormError(f"{self.label}: a whole number, please.") from None
            if self.minimum is not None and value < self.minimum:
                raise FormError(f"{self.label}: at least {self.minimum}.")
            if self.maximum is not None and value > self.maximum:
                raise FormError(f"{self.label}: at most {self.maximum}.")
            return value
        if self.kind == "date":
            try:
                return date.fromisoformat(text)
            except ValueError:
                raise FormError(f"{self.label}: a date as YYYY-MM-DD.") from None
        if self.kind == "choice" and text not in {value for value, _ in self.choices}:
            raise FormError(f"{self.label}: choose one of the options.")
        return text


Values = Mapping[str, Any]


@dataclass(frozen=True)
class Command:
    id: str  # "<area>.<name>"
    area: str  # the rail area it lives in
    title: str
    effect: str  # what happens, before it does: model calls, writes, scale
    module: str  # python -m <module>
    args: Callable[[Values, Path], Sequence[str]]  # values and the job folder → arguments
    fields: tuple[Field, ...] = ()
    job_title: Callable[[Values], str] | None = None
    summarize: Callable[[Path, int], str | None] | None = None
    long_running: bool = False  # runs until stopped (a worker)
    action: str = "Start"  # the button: names what it does

    def parse(self, form: Mapping[str, Any]) -> dict[str, Any]:
        values, problems = {}, []
        for f in self.fields:
            try:
                values[f.name] = f.parse(form.get(f.name))
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


def _copy_log_to(name: str) -> Callable[[Path, int], str | None]:
    def summarize(folder: Path, code: int) -> str | None:
        if code == 0:
            shutil.copyfile(folder / "log.txt", folder / "outputs" / name)
            return name
        return None

    return summarize


def _intake_counts(folder: Path, code: int) -> str | None:
    """The summary JSON intake prints last: ``3 accepted, 0 duplicate``."""
    import json
    import re

    text = (folder / "log.txt").read_text(encoding="utf-8", errors="replace")
    found = re.findall(r"\{[^{}]*\"manifest_ref\"[^{}]*\}", text, flags=re.DOTALL)
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


CATALOGUE: tuple[Command, ...] = (
    Command(
        id="intake.synthetic",
        area="intake",
        title="Synthetic cases",
        effect="Admits synthetic cases (no customer data) under the current release. Nothing calls a model until a worker runs them. A seed used before gives only duplicates.",
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
    ),
    Command(
        id="intake.sql",
        area="intake",
        title="Show the intake query",
        effect="Writes the BigQuery query intake would run, to read or check its cost. Runs nothing.",
        module="cfr_v2.intake",
        fields=(Field("by_case_no", "For a list of case numbers", "check", help="Otherwise, for a date window."),),
        args=lambda v, folder: ["--print-sql", *(["--case-nos-from", "cases.xlsx"] if v["by_case_no"] else [])],
        job_title=lambda v: "Intake query" + (" · by case number" if v["by_case_no"] else " · date window"),
        summarize=_copy_log_to("intake.sql"),
        action="Write the query",
    ),
)

BY_ID: dict[str, Command] = {c.id: c for c in CATALOGUE}


def for_area(area: str) -> list[Command]:
    return [c for c in CATALOGUE if c.area == area]


__all__ = ["BY_ID", "CATALOGUE", "Command", "Field", "FormError", "for_area", "save_upload"]
