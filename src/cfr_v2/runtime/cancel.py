"""Cancel a release's queued case runs, so they never run (LDR-0029).

    python -m cfr_v2.runtime.cancel --release cfr-v2-phase-d --dry-run   # count them only
    python -m cfr_v2.runtime.cancel --release cfr-v2-phase-d             # cancel them

For runs nobody wants to process: test intakes, or runs queued under a release that no longer
matches the code, which stop a worker from starting (LDR-0022). Only queued runs: a running one
belongs to a worker, and stopping that worker hands it back to the queue first.

In one transaction, each run's job leaves the queue (``workflow_job`` is the one table updated in
place: ``FAILED`` with ``last_error`` ``CANCELLED``) and the run records a ``CANCELLED`` state. Nothing
is deleted: the runs, their results so far and their history stay, as everywhere in the state
database. To process such a case after all, admit it again under the current release.
"""

from __future__ import annotations

import argparse
import json
import re

from cfr_v2.telemetry import get_logger

logger = get_logger("cfr_v2.runtime.cancel")  # not __name__: run as -m, that is "__main__"

RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9.-]{1,62}")
_QUEUED = (
    "FROM workflow_job j JOIN case_run c USING (case_run_id)"
    " WHERE j.status = 'QUEUED' AND split_part(c.release_manifest_ref, '@', 1) = %(release)s"
)
COUNT_SQL = f"SELECT count(*) {_QUEUED}"
# SKIP LOCKED: a run a worker is claiming this instant is left to it; FAILED is the queue's
# terminal status (no worker claims it), last_error says why.
CANCEL_SQL = (
    f"WITH picked AS (SELECT j.case_run_id {_QUEUED} FOR UPDATE OF j SKIP LOCKED)"
    " UPDATE workflow_job w SET status = 'FAILED', last_error = 'CANCELLED', updated_at = now()"
    " FROM picked WHERE w.case_run_id = picked.case_run_id RETURNING w.case_run_id"
)
EVENT_SQL = (
    "INSERT INTO case_run_event (case_run_id, state, detail)"
    " SELECT run, 'CANCELLED', %(detail)s::jsonb FROM unnest(%(ids)s::text[]) AS run"
    " ON CONFLICT (case_run_id, state) DO NOTHING"
)


def count_queued(tx, release: str) -> int:
    return int(tx.scalar(COUNT_SQL, {"release": release}) or 0)


def cancel_queued(tx, release: str) -> list[str]:
    """Take ``release``'s queued runs off the queue and record them CANCELLED; their ids."""
    ids = [row[0] for row in tx.all(CANCEL_SQL, {"release": release})]
    if ids:
        detail = json.dumps({"reason": "cancelled while queued", "release": release})
        tx.execute(EVENT_SQL, {"ids": ids, "detail": detail})
    return sorted(ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.runtime.cancel", description=__doc__.splitlines()[0])
    parser.add_argument("--release", required=True, help="the release id, e.g. cfr-v2-phase-d")
    parser.add_argument("--dry-run", action="store_true", help="count the queued runs; change nothing")
    args = parser.parse_args(argv)
    if not RELEASE_ID.fullmatch(args.release):
        parser.error("--release: a release id, lower-case letters, digits, dots and hyphens")

    from cfr_v2.state import Database

    from .settings import RuntimeSettings

    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=1)
    try:
        with db.transaction() as tx:
            if args.dry_run:
                n = count_queued(tx, args.release)
                print(f"{n:,} queued runs under {args.release}; nothing changed (dry run)", flush=True)
                return 0
            ids = cancel_queued(tx, args.release)
    finally:
        db.close()
    logger.info("cancel: %d queued runs under %s cancelled", len(ids), args.release)
    for run_id in ids:
        print(f"cancelled {run_id}", flush=True)
    print(f"{len(ids):,} queued runs under {args.release} cancelled", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
