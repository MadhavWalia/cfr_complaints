"""A local index of what each stage result says, for the Cases filters (LDR-0037).

Claim counts, the primary claim's outcome, the verticals and the claims' grounding live in the
stage results (artifacts), not in Postgres. Filtering on them would mean reading every result on
every page load, so the console keeps the few facts it filters on in a SQLite file in its
workspace (``var/console/case_facts.sqlite``):

- Keyed by the result's ref. A ref is a content address, so its facts never change and the index
  never goes stale: it only grows. Deleting the file is safe; it is rebuilt.
- Counts, enum values and vertical ids only. No claim text, no case text, no identifiers.
- Filled in the background from INTERNAL results only (the reader refuses anything else), with the
  same reader the case page uses. Read-only towards the pipeline.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cfr_v2.contracts import Classification
from cfr_v2.telemetry import get_logger

logger = get_logger("cfr_v2.console.facts")

TASKS = ("c02.pass2", "c03.primary_selection", "c04.routing")  # the results the filters read
READ_THREADS = 8
BATCH = 200  # facts written per SQLite commit
RECHECK_SEC = 10.0  # how often a page visit may look for new results

# Every successful result of the three stages: what the index should hold.
RESULT_REFS_SQL = (
    "SELECT DISTINCT logical_task_id, output_artifact_refs->>0 FROM task_execution"
    " WHERE status = 'SUCCEEDED' AND logical_task_id IN ('c02.pass2', 'c03.primary_selection', 'c04.routing')"
    " AND jsonb_array_length(output_artifact_refs) > 0"
)


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in value or () if isinstance(v, Mapping)] if isinstance(value, list) else []


def facts_of(task: str, data: Any) -> dict[str, Any]:
    """The facts the filters use, from one parsed stage result. Never any text."""
    if not isinstance(data, Mapping):
        return {}
    if task == "c02.pass2":
        claims = _items(data.get("claims"))
        return {
            "claims": len(claims),
            "allegations": len(_items(data.get("allegations"))),
            "grounding": sorted({str(c.get("grounding_status")) for c in claims if c.get("grounding_status")}),
            "accounts": sorted({str(c.get("account_resolution")) for c in claims if c.get("account_resolution")}),
            "baseline": data.get("baseline_profile_ref") is not None,
        }
    if task == "c03.primary_selection":
        return {"primary": data.get("status")}
    if task == "c04.routing":
        claims = _items(data.get("claims"))
        verticals = {
            str(v.get("vertical_id"))
            for c in claims
            if c.get("outcome") == "ROUTED"
            for v in _items(c.get("candidates"))
            if v.get("vertical_id")
        }
        outcomes = {str(c.get("outcome")) for c in claims if c.get("outcome") and c.get("outcome") != "ROUTED"}
        return {"verticals": sorted(verticals | outcomes)}
    return {}


class FactIndex:
    """ref → facts, in SQLite. Safe to share between threads."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS facts (ref TEXT PRIMARY KEY, task TEXT NOT NULL, data TEXT NOT NULL)"
            )

    def known(self) -> set[str]:
        with self._lock:
            return {ref for (ref,) in self._db.execute("SELECT ref FROM facts")}

    def get(self, refs: Iterable[str | None]) -> dict[str, dict[str, Any]]:
        wanted = sorted({r for r in refs if r})
        found: dict[str, dict[str, Any]] = {}
        with self._lock:
            for i in range(0, len(wanted), 500):
                chunk = wanted[i : i + 500]
                marks = ",".join("?" * len(chunk))
                for ref, data in self._db.execute(f"SELECT ref, data FROM facts WHERE ref IN ({marks})", chunk):
                    found[ref] = json.loads(data)
        return found

    def put(self, rows: Iterable[tuple[str, str, dict[str, Any]]]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO facts (ref, task, data) VALUES (?, ?, ?)",
                [(ref, task, json.dumps(data, separators=(",", ":"))) for ref, task, data in rows],
            )

    def verticals(self) -> list[str]:
        """Every vertical (and routing outcome) seen in the indexed C04 results."""
        with self._lock:
            rows = self._db.execute("SELECT data FROM facts WHERE task = 'c04.routing'").fetchall()
        return sorted({v for (data,) in rows for v in json.loads(data).get("verticals", ())})

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class Progress:
    running: bool = False
    done: int = 0
    total: int = 0
    failed: int = 0
    error: str | None = None  # why the last pass stopped, as an exception type


class Indexer:
    """Fills a ``FactIndex`` in a background thread: one pass over the results it doesn't hold yet."""

    def __init__(self, index: FactIndex, *, clock: Callable[[], float] = time.monotonic):
        self.index = index
        self.progress = Progress()
        self._lock = threading.Lock()
        self._clock = clock
        self._checked = float("-inf")

    def pending(self, db) -> list[tuple[str, str]]:
        with db.transaction() as tx:
            rows = tx.all(RESULT_REFS_SQL)
        known = self.index.known()
        return sorted((task, ref) for task, ref in rows if ref and ref.startswith("sha256:") and ref not in known)

    def start(self, db, store, *, wait: bool = False) -> Progress:
        """Look for new results (at most every ``RECHECK_SEC``) and read them in the background."""
        with self._lock:
            if self.progress.running or self._clock() - self._checked < RECHECK_SEC:
                return self.progress
            self._checked = self._clock()
            self.progress = Progress(running=True)
        thread = threading.Thread(target=self._run, args=(db, store), name="case-facts", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return self.progress

    def _run(self, db, store) -> None:
        from .cases import READER, parse_result

        progress = self.progress
        try:
            todo = self.pending(db)
            progress.total = len(todo)

            def read(item: tuple[str, str]) -> tuple[str, str, dict[str, Any]] | None:
                task, ref = item
                try:
                    data = store.get(ref, classification=Classification.INTERNAL, authorization=READER)
                except Exception as exc:  # noqa: BLE001 - one unreadable result is counted, not fatal
                    logger.warning("case facts: result unreadable: %s", type(exc).__name__)
                    return None
                return ref, task, facts_of(task, parse_result(data))

            batch: list[tuple[str, str, dict[str, Any]]] = []
            with ThreadPoolExecutor(READ_THREADS) as pool:
                for found in pool.map(read, todo):
                    progress.done += 1
                    if found is None:
                        progress.failed += 1
                        continue
                    batch.append(found)
                    if len(batch) >= BATCH:
                        self.index.put(batch)
                        batch = []
            self.index.put(batch)
        except Exception as exc:  # noqa: BLE001 - shown on the page as its type; the message can name hosts
            logger.warning("case facts: indexing stopped: %s", type(exc).__name__)
            progress.error = type(exc).__name__
        finally:
            progress.running = False


__all__ = ["TASKS", "FactIndex", "Indexer", "Progress", "facts_of"]
