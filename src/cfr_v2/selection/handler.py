"""The ``c03.primary_selection`` stage handler (LDR-0018).

Reads the claim set from the upstream ``c02.pass2`` artifact (INTERNAL) and nothing else:
no request, transcript, baseline or source record. Returns a ``PrimarySelection`` bound to
the claim set's hash; a later claim-set revision makes it stale (``is_stale``).
"""

from __future__ import annotations

import dspy

from cfr_v2.artifacts import ArtifactStore
from cfr_v2.contracts import AuthorizationContext, ClaimSetRevision, Classification, canonical_json
from cfr_v2.llm import ModelLoop, model_loop
from cfr_v2.llm.dspy_lm import ProgramCall, run_program
from cfr_v2.runtime.stages import StageHandler, StageInput, StageOutput
from cfr_v2.telemetry import set_attributes, span

from .selector import SELECT_PROGRAM_ID, SELECT_VERSION, Selector, build_selection, selector_inputs, shortcut

_READER = AuthorizationContext(principal="c03.primary_selection", purpose="claim set")


def load_claim_set(store: ArtifactStore, ref: str) -> ClaimSetRevision:
    return ClaimSetRevision.model_validate_json(
        store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
    )


def make_selection_handler(store: ArtifactStore, lm: dspy.LM, *, loop: ModelLoop | None = None) -> StageHandler:
    selector = Selector()

    def handle(inp: StageInput) -> StageOutput:
        claim_set = load_claim_set(store, inp.upstream_refs[-1])
        with span("c03.select", program=SELECT_PROGRAM_ID, program_version=SELECT_VERSION) as select_span:
            selection = shortcut(claim_set)
            if selection is None:
                call = ProgramCall(
                    program_id=SELECT_PROGRAM_ID,
                    program_version=SELECT_VERSION,
                    case_id=claim_set.case_id,
                    task_id=inp.stage.logical_task_id,
                    redacted=True,  # claim texts paraphrase redacted text
                    trace_id=inp.trace_id,
                )
                ids = [c.claim_id for c in claim_set.claims]
                prediction = (loop or model_loop()).run(
                    run_program(selector, lm=lm, call=call, claim_ids=ids, **selector_inputs(claim_set))
                )
                selection = build_selection(claim_set, prediction.selection)
            set_attributes(
                select_span,
                claims=len(claim_set.claims),
                selection_status=selection.status.value,
                candidates=len(selection.candidate_claim_ids),
                model_called=len(claim_set.claims) > 1,
            )
        return StageOutput(canonical_json(selection))

    return handle
