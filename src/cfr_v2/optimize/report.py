"""What an optimization run found, and the change that would ship it (LDR-0027 decision 5).

The report compares the baseline and the candidate on the held-out test split, case by case:
the mean difference with a bootstrap interval, and the cases that got better and worse. It
holds case ids, scores and the instructions; no case text.

``patch`` turns the candidate's instructions into a unified diff of the program's source: the
new instructions where the old ones are, and the program version bumped (``pass1-2`` →
``pass1-3``). It is offered only when the candidate is better on test; a person reviews it,
commits it, then makes a release that pins the new version (LDR-0022).
"""

from __future__ import annotations

import difflib
import json
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .prompts import SRC, Prompt

BOOTSTRAP = 2000


@dataclass
class Report:
    prompt_id: str
    program_version: str
    method: str
    budget: str
    seed: int
    model_id: str
    dataset: str  # dataset_hash
    sizes: dict[str, int]  # train, val, test examples
    calls_cap: int
    calls_used: int = 0
    baseline: dict[str, float] = field(default_factory=dict)  # test scores per case
    candidate: dict[str, float] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)  # failed calls on test, baseline and candidate
    instructions_before: str = ""
    instructions_after: str = ""
    seconds: float = 0.0
    stopped: str | None = None  # why the run ended early, if it did

    @property
    def difference(self) -> tuple[float, float, float]:
        return paired_difference(self.baseline, self.candidate)

    @property
    def better(self) -> bool:
        """Better on test, and the interval is above zero."""
        mean, low, _ = self.difference
        return bool(self.candidate) and mean > 0 and low > 0 and self.instructions_after != self.instructions_before

    def changed(self) -> tuple[list[str], list[str]]:
        """Cases that scored higher, and lower, with the candidate."""
        up = sorted(c for c in self.baseline if self.candidate.get(c, 0) > self.baseline[c])
        down = sorted(c for c in self.baseline if self.candidate.get(c, 0) < self.baseline[c])
        return up, down

    def to_json(self) -> str:
        mean, low, high = self.difference
        up, down = self.changed()
        body = asdict(self) | {
            "summary": {
                "baseline_mean": _mean(self.baseline),
                "candidate_mean": _mean(self.candidate),
                "difference": mean,
                "interval_95": [low, high],
                "better": self.better,
                "cases_better": up,
                "cases_worse": down,
            }
        }
        return json.dumps(body, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Report:
        body = json.loads(text)
        body.pop("summary", None)
        return cls(**body)

    def lines(self) -> list[str]:
        """The summary a job prints: counts and scores only."""
        mean, low, high = self.difference
        up, down = self.changed()
        out = [
            (
                f"{self.prompt_id} {self.program_version}: {self.method}, {self.budget} budget, seed {self.seed}, "
                f"model {self.model_id}"
            ),
            (
                f"examples: {self.sizes.get('train', 0)} train, {self.sizes.get('val', 0)} validation, "
                f"{self.sizes.get('test', 0)} test ({self.dataset[:19]}…)"
            ),
            f"model calls: {self.calls_used} of {self.calls_cap}; {self.seconds / 60:.0f} min",
        ]
        if self.stopped:
            out.append(f"stopped early: {self.stopped}")
        if self.baseline:
            out.append(f"test score: baseline {_mean(self.baseline):.3f}")
        if self.candidate:
            out += [
                (
                    f"test score: candidate {_mean(self.candidate):.3f}; difference {mean:+.3f} "
                    f"(95% interval {low:+.3f} to {high:+.3f})"
                ),
                f"cases better: {len(up)}; worse: {len(down)}; same: {len(self.baseline) - len(up) - len(down)}",
                "result: the candidate is better on test: review patch.diff"
                if self.better
                else "result: not better on test: no patch",
            ]
        return out


def _mean(scores: dict[str, float]) -> float:
    return sum(scores.values()) / len(scores) if scores else 0.0


def paired_difference(before: dict[str, float], after: dict[str, float], seed: int = 0) -> tuple[float, float, float]:
    """Mean of (after − before) over the cases both scored, with a 95% bootstrap interval."""
    cases = sorted(set(before) & set(after))
    if not cases:
        return 0.0, 0.0, 0.0
    diffs = [after[c] - before[c] for c in cases]
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(BOOTSTRAP))
    return sum(diffs) / len(diffs), means[int(0.025 * BOOTSTRAP)], means[int(0.975 * BOOTSTRAP) - 1]


def next_version(version: str) -> str:
    """``pass1-2`` → ``pass1-3``."""
    match = re.fullmatch(r"(.*?)(\d+)", version)
    if not match:
        raise ValueError(f"can't bump {version!r}: it doesn't end in a number")
    return f"{match.group(1)}{int(match.group(2)) + 1}"


class PatchError(ValueError):
    pass


def patched(prompt: Prompt, instructions: str, *, source: Path | None = None) -> str:
    """The program's source with ``instructions`` in place of the current ones and the version bumped.
    Refuses text that can't sit inside a triple-quoted string."""
    text = (source or prompt.path).read_text(encoding="utf-8")
    name = (source or prompt.path).name
    new = instructions.strip()
    if '"""' in new or new.endswith("\\"):
        raise PatchError('the instructions contain """ or end in a backslash; edit them by hand')
    if prompt.instructions.startswith("doc:"):
        cls = prompt.instructions[4:]
        found = re.search(rf'(class {cls}\(dspy\.Signature\):\n    """)(.*?)(""")', text, re.DOTALL)
        if not found:
            raise PatchError(f"no docstring for {cls} in {name}")
        indented = "\n".join(("    " + line) if line.strip() else "" for line in new.splitlines()).lstrip()
        body = indented + ("\n    " if found.group(2).endswith("\n    ") else "")
    else:
        found = re.search(rf'(^{prompt.instructions} = """)(.*?)(""")', text, re.DOTALL | re.MULTILINE)
        if not found:
            raise PatchError(f'no {prompt.instructions} = """…""" in {name}')
        old_body = found.group(2)
        body = ("\n" if old_body.startswith("\n") else "") + new + ("\n\n" if old_body.endswith("\n\n") else "")
    changed = text[: found.start(2)] + body + text[found.end(2) :]
    version = re.search(rf'^({prompt.version_constant} = ")([^"]+)(")', changed, re.MULTILINE)
    if not version:
        raise PatchError(f"no {prompt.version_constant} in {name}")
    return changed[: version.start(2)] + next_version(version.group(2)) + changed[version.end(2) :]


def patch(prompt: Prompt, instructions: str, *, source: Path | None = None) -> str:
    """``patched`` as a unified diff against the source, to review and apply with ``git apply``."""
    path = source or prompt.path
    text = path.read_text(encoding="utf-8")
    root = SRC.parents[1]
    name = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    changed = patched(prompt, instructions, source=path)
    return "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True), changed.splitlines(keepends=True), f"a/{name}", f"b/{name}"
        )
    )


__all__ = ["PatchError", "Report", "next_version", "paired_difference", "patch", "patched"]
