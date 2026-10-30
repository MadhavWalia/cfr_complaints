"""Span attribute allowlist (LDR-0012).

Span attributes are passed as short names and exported as ``cfr.<name>``. Only the
names below are exported; anything else is dropped, so transcript text, prompts or
redaction-map content cannot reach traces through an ad-hoc attribute. Adding a
name is a deliberate change: the value must never carry case content.

Metric labels are separate and narrower: lane, stage, outcome and status only,
never a case identifier (one series per case would follow).
"""
from __future__ import annotations

PREFIX = "cfr."

SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # identity (OQ-26: case_no may appear on spans, never on metric labels)
        "case_no",
        "case_run_id",
        "source_system",
        "manifest_ref",
        "workflow",
        # scheduling
        "lane",
        "executor",
        "attempt",
        "max_attempts",
        "reclaimed",
        "queue_wait_sec",
        # stages
        "stage",
        "program_version",
        "checkpoint_reused",
        # outcomes
        "outcome",
        "admission_status",
        "error_type",
        # intake (counts and flags only)
        "rows",
        "has_transcript",
        "redaction_spans",
        "window_start",
        "window_end",
    }
)

METRIC_LABELS: frozenset[str] = frozenset({"lane", "stage", "outcome", "status"})
