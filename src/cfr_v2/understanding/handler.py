"""The ``c02.understanding`` stage handler: Pass 1 for now (Phase C, first slice).

Reads the admitted request for its refs only, loads the redacted transcript
(INTERNAL), runs Pass 1 on the process's model loop and returns the
``AllegationSet``. Nothing else from the request reaches the program: no case
metadata, baseline or evaluation labels (ADR-03, OQ-21).
"""

from __future__ import annotations

import dspy

from cfr_v2.artifacts import ArtifactStore
from cfr_v2.contracts import (
    AllegationSet,
    AuthorizationContext,
    CanonicalTranscript,
    Classification,
    IngestionRequest,
    canonical_json,
)
from cfr_v2.llm import ModelLoop, model_loop
from cfr_v2.llm.dspy_lm import ProgramCall, run_program
from cfr_v2.runtime.stages import StageHandler, StageInput, StageOutput
from cfr_v2.telemetry import set_attributes, span

from .pass1 import PASS1_PROGRAM_ID, PASS1_VERSION, Pass1, build_allegation_set, transcript_text

# The request is RESTRICTED (it holds the evaluation labels); only its refs are used.
_REQUEST_READER = AuthorizationContext(
    principal="c02.understanding", purpose="read request refs", allowed_classifications=frozenset({"RESTRICTED"})
)
_TRANSCRIPT_READER = AuthorizationContext(principal="c02.understanding", purpose="pass1 input")


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


def make_understanding_handler(
    store: ArtifactStore, lm: dspy.LM, *, loop: ModelLoop | None = None, program: Pass1 | None = None
) -> StageHandler:
    pass1 = program or Pass1()

    def handle(inp: StageInput) -> StageOutput:
        request = IngestionRequest.model_validate_json(
            store.get(inp.request_ref, classification=Classification.RESTRICTED, authorization=_REQUEST_READER)
        )
        case_id = request.key.source_case_id
        with span("c02.pass1", program=PASS1_PROGRAM_ID, program_version=PASS1_VERSION) as pass1_span:
            if request.transcript_ref is None:
                result = AllegationSet(
                    case_id=case_id,
                    program_id=PASS1_PROGRAM_ID,
                    program_version=PASS1_VERSION,
                    transcript_ref=None,
                    allegations=(),
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
                result = build_allegation_set(
                    prediction.allegations, segments, case_id=case_id, transcript_ref=request.transcript_ref
                )
                set_attributes(
                    pass1_span,
                    has_transcript=True,
                    allegations=len(result.allegations),
                    allegations_dropped=len(result.dropped),
                )
        return StageOutput(canonical_json(result))

    return handle
