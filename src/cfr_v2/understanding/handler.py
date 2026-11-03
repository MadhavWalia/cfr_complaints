"""C02 stage handlers: ``c02.pass1`` and ``c02.pass2`` (two stages, so a Pass 2 retry never repeats Pass 1).

Pass 1 reads the admitted request for its refs only and loads the redacted transcript
(INTERNAL); nothing else reaches the program: no case metadata, baseline or evaluation
labels (ADR-03, OQ-21). It returns a ``Pass1Result``.

Pass 2 reads Pass 1's result (its upstream artifact), the admission-time baseline
(INTERNAL) and the case received date, taken in code from the source record's
``rcpt_ts`` so every case has one, baseline or not. The model sees the date only. It
returns the ``ClaimSetRevision``.
"""

from __future__ import annotations

import json
import threading
from datetime import date

import dspy

from cfr_v2.artifacts import ArtifactStore
from cfr_v2.contracts import (
    AuthorizationContext,
    BaselineProfile,
    CanonicalTranscript,
    Classification,
    EvidenceSourceTaxonomy,
    IngestionRequest,
    Pass1Result,
    canonical_json,
)
from cfr_v2.llm import ModelLoop, model_loop
from cfr_v2.llm.dspy_lm import ProgramCall, run_program
from cfr_v2.registry.evidence_sources import TaxonomySource
from cfr_v2.runtime.stages import StageHandler, StageInput, StageOutput
from cfr_v2.telemetry import set_attributes, span

from .pass1 import PASS1_PROGRAM_ID, PASS1_VERSION, Pass1, build_pass1_result, transcript_text
from .pass2 import PASS2_PROGRAM_ID, PASS2_VERSION, Pass2, build_claim_set, program_inputs

# The request is RESTRICTED (it holds the evaluation labels); only its refs are used.
_REQUEST_READER = AuthorizationContext(
    principal="c02.understanding", purpose="read request refs", allowed_classifications=frozenset({"RESTRICTED"})
)
_TRANSCRIPT_READER = AuthorizationContext(principal="c02.pass1", purpose="pass1 input")
_PASS2_READER = AuthorizationContext(principal="c02.pass2", purpose="pass2 input")
# The source record is RESTRICTED; Pass 2 takes the received date from it and nothing else.
_RECEIVED_DATE_READER = AuthorizationContext(
    principal="c02.pass2", purpose="case received date", allowed_classifications=frozenset({"RESTRICTED"})
)


def load_request(store: ArtifactStore, request_ref: str) -> IngestionRequest:
    return IngestionRequest.model_validate_json(
        store.get(request_ref, classification=Classification.RESTRICTED, authorization=_REQUEST_READER)
    )


def load_segments(store: ArtifactStore, transcript_ref: str) -> list[tuple[str, str]]:
    """``(interaction_id, redacted_text)`` per interaction, in order."""
    transcript = CanonicalTranscript.model_validate_json(
        store.get(transcript_ref, classification=Classification.INTERNAL, authorization=_TRANSCRIPT_READER)
    )
    return [
        (
            i.interaction_id,
            store.get(
                i.redacted_text_ref, classification=Classification.INTERNAL, authorization=_TRANSCRIPT_READER
            ).decode("utf-8"),
        )
        for i in transcript.interactions
    ]


def received_date(store: ArtifactStore, request: IngestionRequest, baseline: BaselineProfile | None) -> date | None:
    """``rcpt_ts`` from the source record, else the baseline's copy of it."""
    if request.source_record_ref is not None:
        record = json.loads(
            store.get(
                request.source_record_ref, classification=Classification.RESTRICTED, authorization=_RECEIVED_DATE_READER
            )
        )
        value = record.get("rcpt_ts")
        if isinstance(value, str) and len(value) >= 10:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                pass
    return baseline.case_received_date if baseline else None


def make_pass1_handler(
    store: ArtifactStore, lm: dspy.LM, *, loop: ModelLoop | None = None, program: Pass1 | None = None
) -> StageHandler:
    pass1 = program or Pass1()

    def handle(inp: StageInput) -> StageOutput:
        request = load_request(store, inp.request_ref)
        case_id = request.key.source_case_id
        with span("c02.pass1", program=PASS1_PROGRAM_ID, program_version=PASS1_VERSION) as pass1_span:
            if request.transcript_ref is None:
                result = Pass1Result(
                    case_id=case_id, program_id=PASS1_PROGRAM_ID, program_version=PASS1_VERSION, transcript_ref=None
                )
                set_attributes(pass1_span, has_transcript=False)
            else:
                segments = load_segments(store, request.transcript_ref)
                call = ProgramCall(
                    program_id=PASS1_PROGRAM_ID,
                    program_version=PASS1_VERSION,
                    case_id=case_id,
                    task_id=inp.stage.logical_task_id,
                    redacted=True,  # only redacted_text_ref content is sent
                    trace_id=inp.trace_id,
                )
                prediction = (loop or model_loop()).run(
                    run_program(pass1, lm=lm, call=call, transcript=transcript_text(segments))
                )
                result = build_pass1_result(
                    prediction.allegations,
                    prediction.requests,
                    prediction.agent_statements,
                    segments,
                    case_id=case_id,
                    transcript_ref=request.transcript_ref,
                )
                set_attributes(
                    pass1_span,
                    has_transcript=True,
                    allegations=len(result.allegations),
                    requests=len(result.requests),
                    agent_statements=len(result.agent_statements),
                    items_dropped=len(result.dropped),
                )
        return StageOutput(canonical_json(result))

    return handle


def make_pass2_handler(
    store: ArtifactStore,
    lm: dspy.LM,
    taxonomy: TaxonomySource,
    *,
    loop: ModelLoop | None = None,
) -> StageHandler:
    """``taxonomy``: a fixed taxonomy, or a resolver from the run's manifest ref (``PinnedTaxonomies``)."""
    programs: dict[str, Pass2] = {}  # one program per taxonomy: its instructions and output enum differ
    lock = threading.Lock()

    def program_for(t: EvidenceSourceTaxonomy) -> Pass2:
        with lock:
            if t.taxonomy_hash not in programs:
                programs[t.taxonomy_hash] = Pass2(t)
            return programs[t.taxonomy_hash]

    def handle(inp: StageInput) -> StageOutput:
        request = load_request(store, inp.request_ref)
        pass1 = Pass1Result.model_validate_json(
            store.get(inp.upstream_refs[-1], classification=Classification.INTERNAL, authorization=_PASS2_READER)
        )
        baseline = None
        if request.baseline_ref is not None:
            baseline = BaselineProfile.model_validate_json(
                store.get(request.baseline_ref, classification=Classification.INTERNAL, authorization=_PASS2_READER)
            )
        received = received_date(store, request, baseline)
        run_taxonomy = taxonomy if isinstance(taxonomy, EvidenceSourceTaxonomy) else taxonomy(inp.release_manifest_ref)
        pass2 = program_for(run_taxonomy)
        # The taxonomy is part of what the model is told; a different one is a different program input.
        version = f"{PASS2_VERSION}+{run_taxonomy.version}@{run_taxonomy.taxonomy_hash[:12]}"
        with span("c02.pass2", program=PASS2_PROGRAM_ID, program_version=PASS2_VERSION) as pass2_span:
            claims, checks = (), ()
            if pass1.allegations or pass1.requests or pass1.agent_statements:
                call = ProgramCall(
                    program_id=PASS2_PROGRAM_ID,
                    program_version=version,
                    case_id=pass1.case_id,
                    task_id=inp.stage.logical_task_id,
                    redacted=True,  # Pass 1's paraphrases of redacted text, and the approved baseline
                    trace_id=inp.trace_id,
                )
                prediction = (loop or model_loop()).run(
                    run_program(pass2, lm=lm, call=call, **program_inputs(pass1, baseline, received))
                )
                claims, checks = prediction.claims, prediction.other_checks
            result = build_claim_set(
                pass1,
                claims,
                checks,
                run_id=inp.case_run_id,
                baseline=baseline,
                baseline_ref=request.baseline_ref,
                received=received,
                taxonomy=run_taxonomy,
            )
            set_attributes(
                pass2_span,
                has_baseline=baseline is not None,
                claims=len(result.claims),
                evidence_needs=sum(len(c.evidence_needs) for c in result.claims),
                other_checks=len(result.other_checks),
                findings=len(result.findings),
            )
        return StageOutput(canonical_json(result))

    return handle
