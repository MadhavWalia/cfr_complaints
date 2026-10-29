from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cfr_v2.contracts import (
    AdmissionKey,
    AdmissionRecord,
    AdmissionStatus,
    Allegation,
    AuthorizationContext,
    ClaimRouting,
    ClaimRoutingOutcome,
    ClaimScope,
    ClaimSetRevision,
    ExecutionPlan,
    GroundedClaim,
    GroundingStatus,
    IngestionRequest,
    PlannerSource,
    PlanRevision,
    PlanRevisionTrigger,
    PlanValidationReport,
    PrimarySelection,
    RedactionMap,
    RedactionSpan,
    ReleaseManifest,
    SelectionStatus,
    SpanRef,
    StatementKind,
    TaskEdge,
    TaskEnvelope,
    TaskKind,
    TaskNode,
    TaskResult,
    TaskStatus,
    ValidationIssue,
)

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
H = "a" * 64


def _allegation(aid="a1"):
    return Allegation(
        allegation_id=aid,
        text="card was declined",
        kind=StatementKind.CLAIM,
        scope=ClaimScope.CUSTOMER,
        spans=(SpanRef(interaction_id="t", start=0, end=4),),
    )


def _claim(cid="c1", aids=("a1",)):
    return GroundedClaim(
        claim_id=cid, allegation_ids=aids, statement="decline", grounding_status=GroundingStatus.GROUNDED
    )


def _claim_set(allegations=None, claims=None):
    allegations = allegations or (_allegation(),)
    claims = claims or (_claim(),)
    return ClaimSetRevision(
        case_id="case",
        run_id="run",
        revision=0,
        allegations=allegations,
        claims=claims,
        claim_set_hash=ClaimSetRevision.compute_hash(allegations, claims),
    )


# -- base behaviour --------------------------------------------------------------


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        AdmissionKey(source_system="icare", source_case_id="1", ingestion_revision=0, extra=1)


def test_models_are_frozen():
    key = AdmissionKey(source_system="icare", source_case_id="1", ingestion_revision=0)
    with pytest.raises(ValidationError):
        key.source_case_id = "2"


def test_round_trip_json():
    cs = _claim_set()
    assert ClaimSetRevision.model_validate_json(cs.model_dump_json()) == cs


# -- execution ---------------------------------------------------------------------


def test_task_envelope_requires_aware_deadline():
    with pytest.raises(ValidationError):
        TaskEnvelope(
            task_id="t",
            run_id="r",
            case_id="c",
            capability_id="x",
            capability_version="1",
            input_snapshot_hash=H,
            release_manifest_ref="rel",
            authorization=AuthorizationContext(principal="p", purpose="cfr"),
            idempotency_key="k",
            deadline_at=datetime(2026, 1, 1),
        )


@pytest.mark.parametrize("status", [TaskStatus.FAILED_RETRYABLE, TaskStatus.FAILED_PERMANENT])
def test_failed_result_needs_error_code(status):
    with pytest.raises(ValidationError, match="error_code"):
        TaskResult(task_id="t", status=status)


def test_succeeded_result_rejects_error_code():
    with pytest.raises(ValidationError):
        TaskResult(task_id="t", status=TaskStatus.SUCCEEDED, error_code="X")


# -- intake ------------------------------------------------------------------------


def test_labels_cannot_double_as_metadata():
    with pytest.raises(ValidationError, match="both metadata and labels"):
        IngestionRequest(
            key=AdmissionKey(source_system="icare", source_case_id="1", ingestion_revision=0),
            raw_transcript_ref=None,
            case_metadata={"potl_defect_flag": "Yes"},
            evaluation_labels={"potl_defect_flag": "Yes"},
            received_at=NOW,
        )


def test_rejected_admission_has_no_run_and_needs_reason():
    key = AdmissionKey(source_system="icare", source_case_id="1", ingestion_revision=0)
    with pytest.raises(ValidationError):
        AdmissionRecord(admission_id="a", key=key, status=AdmissionStatus.REJECTED, case_id="c", case_run_id="r")
    with pytest.raises(ValidationError):
        AdmissionRecord(admission_id="a", key=key, status=AdmissionStatus.REJECTED, case_id="c", case_run_id=None)
    with pytest.raises(ValidationError):
        AdmissionRecord(admission_id="a", key=key, status=AdmissionStatus.DUPLICATE, case_id="c", case_run_id=None)


def test_span_must_be_non_empty():
    with pytest.raises(ValidationError):
        SpanRef(interaction_id="t", start=5, end=5)


def _redaction_map():
    # original: "card 4111222233334444 declined"  (16-digit PAN at 5..21)
    # redacted: "card *** declined"               (token at 5..8)
    return RedactionMap(
        interaction_id="t",
        original_sha256=H,
        redacted_sha256=H,
        spans=(RedactionSpan(original_start=5, original_end=21, redacted_start=5, redacted_end=8),),
        redaction_version="v1",
    )


@pytest.mark.parametrize(
    "redacted, original",
    [
        ((0, 4), (0, 4)),  # before the token
        ((9, 17), (22, 30)),  # after the token: shifted by +13
        ((5, 8), (5, 21)),  # exactly the token
        ((6, 7), (5, 21)),  # inside the token expands to the whole value
        ((0, 17), (0, 30)),  # spans the token
        ((3, 6), (3, 21)),  # end inside the token
        ((7, 12), (5, 25)),  # start inside the token
    ],
)
def test_redaction_map_translates_offsets(redacted, original):
    assert _redaction_map().to_original(*redacted) == original


def test_redaction_map_rejects_overlap():
    spans = (
        RedactionSpan(original_start=0, original_end=10, redacted_start=0, redacted_end=3),
        RedactionSpan(original_start=5, original_end=12, redacted_start=2, redacted_end=5),
    )
    with pytest.raises(ValidationError, match="non-overlapping"):
        RedactionMap(interaction_id="t", original_sha256=H, redacted_sha256=H, spans=spans, redaction_version="v1")


# -- understanding -----------------------------------------------------------------


def test_claim_set_hash_must_match_content():
    with pytest.raises(ValidationError, match="claim_set_hash"):
        ClaimSetRevision(
            case_id="c", run_id="r", revision=0, allegations=(_allegation(),), claims=(_claim(),), claim_set_hash=H
        )


def test_claim_set_hash_ignores_order():
    a = (_allegation("a1"), _allegation("a2"))
    c = (_claim("c1", ("a1",)), _claim("c2", ("a2",)))
    assert ClaimSetRevision.compute_hash(a, c) == ClaimSetRevision.compute_hash(a[::-1], c[::-1])


def test_claim_references_known_allegations():
    with pytest.raises(ValidationError, match="unknown allegations"):
        _claim_set(claims=(_claim(aids=("missing",)),))


# -- selection ---------------------------------------------------------------------


def _selection(**kw):
    base = dict(rationale="r", program_id="p", program_version="1", claim_set_hash=H)
    return PrimarySelection(**(base | kw))


def test_selected_requires_claim_id():
    with pytest.raises(ValidationError):
        _selection(status=SelectionStatus.SELECTED)


def test_ambiguous_requires_two_candidates():
    with pytest.raises(ValidationError):
        _selection(status=SelectionStatus.AMBIGUOUS, candidate_claim_ids=("c1",))
    assert _selection(status=SelectionStatus.AMBIGUOUS, candidate_claim_ids=("c1", "c2"))


def test_selection_staleness():
    sel = _selection(status=SelectionStatus.SELECTED, claim_id="c1")
    assert not sel.is_stale(H)
    assert sel.is_stale("b" * 64)


# -- routing -----------------------------------------------------------------------


def test_routed_requires_candidates_and_others_forbid_them():
    with pytest.raises(ValidationError):
        ClaimRouting(claim_id="c1", outcome=ClaimRoutingOutcome.ROUTED)
    assert ClaimRouting(claim_id="c1", outcome=ClaimRoutingOutcome.UNMAPPED)


# -- planning ----------------------------------------------------------------------


def _node(nid, kind=TaskKind.RETRIEVAL):
    return TaskNode(node_id=nid, kind=kind, capability_id="cap", capability_version="1")


def _plan(nodes, edges=()):
    return ExecutionPlan(run_id="r", revision=0, claim_set_hash=H, planner=PlannerSource.TEMPLATE, nodes=nodes, edges=edges)


def test_plan_rejects_cycle():
    with pytest.raises(ValidationError, match="cycle"):
        _plan((_node("a"), _node("b")), (TaskEdge(upstream="a", downstream="b"), TaskEdge(upstream="b", downstream="a")))


def test_plan_rejects_unknown_edge_node():
    with pytest.raises(ValidationError, match="unknown node"):
        _plan((_node("a"),), (TaskEdge(upstream="a", downstream="z"),))


def test_topological_order_is_deterministic():
    plan = _plan(
        (_node("d"), _node("b"), _node("a"), _node("c")),
        (TaskEdge(upstream="a", downstream="c"), TaskEdge(upstream="b", downstream="c"), TaskEdge(upstream="c", downstream="d")),
    )
    assert plan.topological_order() == ["a", "b", "c", "d"]


def test_plan_revision_lineage():
    assert PlanRevision(run_id="r", revision=0, trigger=PlanRevisionTrigger.INITIAL)
    with pytest.raises(ValidationError):
        PlanRevision(run_id="r", revision=1, trigger=PlanRevisionTrigger.INITIAL, previous_revision=0)
    with pytest.raises(ValidationError):
        PlanRevision(run_id="r", revision=1, trigger=PlanRevisionTrigger.CLAIM_SET_REVISED, previous_revision=1)


def test_validation_report_consistency():
    with pytest.raises(ValidationError):
        PlanValidationReport(run_id="r", revision=0, valid=False)
    with pytest.raises(ValidationError):
        PlanValidationReport(run_id="r", revision=0, valid=True, issues=(ValidationIssue(code="X", message="m"),))


# -- registry ----------------------------------------------------------------------


def test_manifest_checksum_computed_and_verified():
    m = ReleaseManifest.build(release_id="rel-1", capabilities=("policy.search@1",))
    assert ReleaseManifest.model_validate_json(m.model_dump_json()) == m
    tampered = m.model_dump() | {"capabilities": ("policy.search@2",)}
    with pytest.raises(ValidationError, match="checksum"):
        ReleaseManifest(**tampered)


def test_manifest_checksum_depends_on_content():
    a = ReleaseManifest.build(release_id="rel-1")
    b = ReleaseManifest.build(release_id="rel-2")
    assert a.checksum != b.checksum
    assert a.ref.startswith("rel-1@")
