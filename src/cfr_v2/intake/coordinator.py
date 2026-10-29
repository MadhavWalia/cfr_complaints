"""C01 admission coordinator: batch admission and a single-case ingress entry point.

The release manifest is resolved and pinned once, when the coordinator is built,
so every case in a batch is pinned to the same manifest (no mid-batch change).
Requires a started runtime (``cfr_v2.runtime.start_runtime``); a submit-only
process uses ``listen_lanes=()``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from cfr_v2 import runtime
from cfr_v2.artifacts import ArtifactStore
from cfr_v2.contracts import Lane, ReleaseManifest, canonical_hash, sha256_hex
from cfr_v2.registry import pin_manifest
from cfr_v2.telemetry import get_logger

from .prepare import admission_key, prepare_case
from .redaction import Masker, ProfanityFilter
from .source import InvalidSourceRowError, source_record_bytes

logger = get_logger(__name__)


class IntakeOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"  # recorded in ``admission``; a later re-run may still admit the case
    UNIDENTIFIED = "UNIDENTIFIED"  # no case_no, so nothing can be recorded against a case
    FAILED = "FAILED"  # infrastructure error; nothing recorded, safe to retry


@dataclass(frozen=True)
class RowResult:
    source_case_id: str | None
    outcome: IntakeOutcome
    case_run_id: str | None = None
    reason: str | None = None
    has_transcript: bool | None = None


@dataclass(frozen=True)
class BatchReport:
    manifest_ref: str
    results: tuple[RowResult, ...]

    def count(self, outcome: IntakeOutcome) -> int:
        return sum(r.outcome is outcome for r in self.results)

    def summary(self) -> dict[str, int]:
        return {o.value: self.count(o) for o in IntakeOutcome} | {
            "NO_TRANSCRIPT": sum(r.has_transcript is False for r in self.results)
        }


class IntakeCoordinator:
    def __init__(
        self,
        *,
        store: ArtifactStore,
        masker: Masker,
        manifest_ref: str,
        profanity: ProfanityFilter | None = None,
        max_workers: int = 8,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.masker = masker
        self.profanity = profanity or ProfanityFilter()
        self.manifest_ref = manifest_ref
        self.max_workers = max_workers
        self.clock = clock

    @classmethod
    def pinned(cls, manifest: ReleaseManifest, *, store: ArtifactStore, **kwargs: Any) -> "IntakeCoordinator":
        """Pin ``manifest`` (idempotent) and build a coordinator that admits under it."""
        ref = pin_manifest(manifest, store=store, db=runtime.current().db)
        logger.info("intake: pinned release manifest %s", ref)
        return cls(store=store, manifest_ref=ref, **kwargs)

    def admit_rows(self, rows: Iterable[Mapping[str, Any]], *, lane: Lane = Lane.BATCH) -> BatchReport:
        """Admit a batch. Each row succeeds or fails on its own; the report lists every outcome."""
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="cfr-intake") as pool:
            results = tuple(pool.map(lambda row: self.admit_case(row, lane=lane), rows))
        report = BatchReport(self.manifest_ref, results)
        logger.info("intake: batch done %s", report.summary())
        return report

    def admit_case(self, row: Mapping[str, Any], *, lane: Lane = Lane.REALTIME) -> RowResult:
        """Admit one case (also the API ingress entry point)."""
        try:
            key = admission_key(row)
        except InvalidSourceRowError as exc:
            logger.warning("intake: row without case id (%s)", exc.reason)
            return RowResult(None, IntakeOutcome.UNIDENTIFIED, reason=exc.reason)
        case_no = key.source_case_id
        try:
            prepared = prepare_case(
                row, store=self.store, masker=self.masker, profanity=self.profanity, lane=lane, received_at=self.clock()
            )
            record, _ = runtime.submit(
                prepared.request,
                release_manifest_ref=self.manifest_ref,
                artifacts=prepared.artifacts,
                admitted_detail={"transcript": "present" if prepared.has_transcript else "absent"},
            )
        except InvalidSourceRowError as exc:
            runtime.reject(key, reason=exc.reason, request_hash=_row_hash(row, exc.reason))
            logger.warning("intake: case %s rejected (%s)", case_no, exc.reason)
            return RowResult(case_no, IntakeOutcome.REJECTED, reason=exc.reason)
        except Exception as exc:  # reported per row; never log row content
            logger.exception("intake: case %s failed (%s)", case_no, type(exc).__name__)
            return RowResult(case_no, IntakeOutcome.FAILED, reason=type(exc).__name__)
        return RowResult(
            case_no,
            IntakeOutcome(record.status.value),
            case_run_id=record.case_run_id,
            reason=record.reason,
            has_transcript=prepared.has_transcript,
        )


def _row_hash(row: Mapping[str, Any], reason: str) -> str:
    try:
        return sha256_hex(source_record_bytes(row))
    except InvalidSourceRowError:
        return canonical_hash({"case_no": str(row.get("case_no")), "reason": reason})
