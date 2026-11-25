"""Retire case runs, or restore them (LDR-0045).

    python -m cfr_v2.runtime.retire retire  --runs-file runs.txt [--reason "phase-d runs"]
    python -m cfr_v2.runtime.retire restore --runs-file runs.txt

A retired run is hidden: from the Cases list (unless asked for), the evaluation exports, the
pipeline counts and re-run selection. Nothing is deleted. Retiring adds a row to
``case_run_retirement``, and restoring adds another, so both are on record with when and why.

A run a worker holds or will take (queued or running) can't be retired: cancel it first. Output is
counts and ids, never case text.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REASON_MAX = 200  # the table's own limit

STATES_SQL = (
    "SELECT r.case_run_id, j.status, x.case_run_id IS NOT NULL"
    " FROM case_run r LEFT JOIN workflow_job j USING (case_run_id)"
    " LEFT JOIN case_run_retired x ON x.case_run_id = r.case_run_id"
    " WHERE r.case_run_id = ANY(%(runs)s)"
)
INSERT_SQL = (
    "INSERT INTO case_run_retirement (case_run_id, retired, reason)"
    " SELECT run, %(retired)s, %(reason)s FROM unnest(%(runs)s::text[]) AS run"
)


@dataclass(frozen=True)
class Found:
    case_run_id: str
    job_status: str | None
    retired: bool


def decide(action: str, given: Sequence[str], found: Sequence[Found]) -> dict[str, list[str]]:
    """Which runs change, and why the rest don't: ``{"changed": [...], "<reason>": [...]}``."""
    by_id = {f.case_run_id: f for f in found}
    out: dict[str, list[str]] = {"changed": []}
    for run in dict.fromkeys(given):
        f = by_id.get(run)
        if f is None:
            reason = "not a case run here"
        elif action == "retire" and f.retired:
            reason = "already retired"
        elif action == "retire" and f.job_status in ("QUEUED", "RUNNING"):
            reason = "queued or running: cancel it first"
        elif action == "restore" and not f.retired:
            reason = "not retired"
        else:
            out["changed"].append(run)
            continue
        out.setdefault(reason, []).append(run)
    return out


def apply(tx: Any, action: str, runs: Sequence[str], reason: str | None, *, write: bool = True) -> dict[str, list[str]]:
    """Retire or restore ``runs`` in the caller's transaction, as ``decide`` decides. The runs are
    locked first, so two jobs on the same runs record each change once. ``write=False``: decide only."""
    rows = tx.all(STATES_SQL + (" FOR UPDATE OF r" if write else ""), {"runs": list(runs)})
    decided = decide(action, runs, [Found(r[0], r[1], bool(r[2])) for r in rows])
    if write and decided["changed"]:
        tx.execute(
            INSERT_SQL,
            {
                "retired": action == "retire",
                "reason": reason if action == "retire" else None,
                "runs": decided["changed"],
            },
        )
    return decided


def summary(action: str, decided: dict[str, list[str]]) -> list[str]:
    done = "retired" if action == "retire" else "restored"
    lines = [f"{len(v):,} skipped: {k}" for k, v in decided.items() if k != "changed" and v]
    return [*lines, f"{len(decided['changed']):,} runs {done}"]


def _runs(path: Path) -> list[str]:
    ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return list(dict.fromkeys(i for i in ids if i and not i.startswith("#")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.runtime.retire", description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("retire", "restore"))
    parser.add_argument("--runs-file", type=Path, required=True, help="case run ids, one per line")
    parser.add_argument("--reason", help=f"why (retire only): a few words, at most {REASON_MAX} characters")
    parser.add_argument("--count-only", action="store_true", help="say what would change; change nothing")
    args = parser.parse_args(argv)
    runs = _runs(args.runs_file)
    if not runs:
        parser.exit(2, f"error: no case run ids in {args.runs_file}\n")
    reason = " ".join((args.reason or "").split()) or None
    if reason and args.action == "restore":
        parser.exit(2, "error: --reason goes with retire\n")
    if reason and len(reason) > REASON_MAX:
        parser.exit(2, f"error: --reason: at most {REASON_MAX} characters\n")

    from cfr_v2.state import Database

    from .settings import RuntimeSettings

    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=1)
    try:
        with db.transaction() as tx:
            decided = apply(tx, args.action, runs, reason, write=not args.count_only)
    finally:
        db.close()
    lines = summary(args.action, decided)
    if args.count_only:
        lines[-1] = lines[-1].replace(" runs ", " runs would be ", 1)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
