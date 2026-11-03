"""Telemetry Part 1 on the real stack (LDR-0012): one trace per case run carried
through the job row, runner and intake spans, bounded metric labels, and no case
content in spans or logs."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from cfr_v2.contracts import Lane
from cfr_v2.intake import IntakeCoordinator, ProfanityFilter
from cfr_v2.intake.fakes import RegexMasker, synthetic_names, synthetic_rows
from cfr_v2.registry import DEFAULT_REGISTRY_DIR, ReleaseRegistry
from cfr_v2.runtime import FINAL_STATE, STAGES, StageInput, StageOutput, submit
from cfr_v2.runtime import metrics as runtime_metrics
from cfr_v2.runtime.stages import placeholder_handler
from cfr_v2.telemetry import METRIC_LABELS, SPAN_ATTRIBUTES, span

from .spike.conftest import make_request, wait_for_state

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[2]


def _wait_for(spans, name: str, *, count: int = 1, timeout: float = 10.0) -> list:
    """Spans named ``name`` once ``count`` have ended (a run's job settles before its span closes)."""
    deadline = time.monotonic() + timeout
    while True:
        found = [s for s in spans.get_finished_spans() if s.name == name]
        if len(found) >= count:
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"{len(found)} of {count} {name!r} spans ended")
        time.sleep(0.05)


def _trace_context(rt, run_id: str) -> str | None:
    with rt.db.transaction() as tx:
        return tx.scalar("SELECT trace_context FROM workflow_job WHERE case_run_id = %(r)s", {"r": run_id})


def test_case_run_is_one_trace_linked_to_the_caller(running, spans):
    seen_trace_ids: list[str | None] = []

    def capturing(inp: StageInput) -> StageOutput:
        seen_trace_ids.append(inp.trace_id)
        return placeholder_handler(inp)

    running.handlers = {st.logical_task_id: capturing for st in STAGES}
    with span("caller"):
        record, handle = submit(make_request("trace-1"), release_manifest_ref="spike@0")
    assert handle.get_result() == FINAL_STATE

    (attempt,) = _wait_for(spans, "case_run.attempt")
    (admit,) = _wait_for(spans, "case_run.admit")
    (caller,) = _wait_for(spans, "caller")
    trace_id = admit.context.trace_id

    # Root of its own trace, linked back to the caller's span.
    assert admit.parent is None and trace_id != caller.context.trace_id
    assert admit.links[0].context.span_id == caller.context.span_id
    assert admit.attributes["cfr.case_no"] == "trace-1"
    assert admit.attributes["cfr.admission_status"] == "ACCEPTED"

    # The job row carries the admit span; the attempt continues it.
    assert _trace_context(running, record.case_run_id) == f"00-{trace_id:032x}-{admit.context.span_id:016x}-01"
    assert attempt.context.trace_id == trace_id and attempt.parent.span_id == admit.context.span_id
    assert attempt.attributes["cfr.attempt"] == 1
    assert attempt.attributes["cfr.reclaimed"] is False
    assert attempt.attributes["cfr.outcome"] == "SUCCEEDED"

    stage_spans = [s for s in spans.get_finished_spans() if s.name.startswith("stage.")]
    assert [s.attributes["cfr.stage"] for s in stage_spans] == [st.logical_task_id for st in STAGES]
    assert all(s.parent.span_id == attempt.context.span_id for s in stage_spans)
    assert {s.attributes["cfr.outcome"] for s in stage_spans} == {"committed"}
    assert seen_trace_ids == [f"{trace_id:032x}"] * len(STAGES)  # what Phase C puts in TaskEnvelope.trace_id


def test_retried_run_keeps_its_trace(running, spans, monkeypatch):
    from cfr_v2.runtime import workflow

    real_commit = workflow._commit
    failures = {"left": 1}

    def flaky_commit(repo, run, stage, *args):
        if stage.logical_task_id == "c04.routing" and failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("database went away")
        return real_commit(repo, run, stage, *args)

    monkeypatch.setattr(workflow, "_commit", flaky_commit)
    _, handle = submit(make_request("trace-retry"), release_manifest_ref="spike@0")
    assert handle.get_result() == FINAL_STATE

    first, second = sorted(_wait_for(spans, "case_run.attempt", count=2), key=lambda s: s.attributes["cfr.attempt"])
    (admit,) = _wait_for(spans, "case_run.admit")
    assert first.context.trace_id == second.context.trace_id == admit.context.trace_id
    assert first.attributes["cfr.outcome"] == "RETRY"
    assert first.attributes["cfr.error_type"] == "ConnectionError"
    assert second.attributes["cfr.outcome"] == "SUCCEEDED"
    # Stages committed on attempt 1 are skipped on attempt 2; c04 reuses its produce checkpoint.
    c04 = [s for s in spans.get_finished_spans() if s.name == "stage.c04.routing"]
    assert [s.attributes.get("cfr.checkpoint_reused") for s in c04] == [False, True]


def test_produce_retries_are_span_events(running, spans):
    calls = {"n": 0}

    def flaky(inp: StageInput) -> StageOutput:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("gateway slow")
        return placeholder_handler(inp)

    running.handlers = {**running.handlers, "c02.pass1": flaky}
    _, handle = submit(make_request("trace-produce"), release_manifest_ref="spike@0")
    assert handle.get_result() == FINAL_STATE
    _wait_for(spans, "case_run.attempt")
    (produce,) = [
        s for s in spans.get_finished_spans() if s.name == "produce" and s.attributes["cfr.stage"] == "c02.pass1"
    ]
    assert [(e.name, dict(e.attributes)) for e in produce.events] == [
        ("retry", {"cfr.attempt": 1, "cfr.error_type": "TimeoutError"})
    ]
    assert produce.attributes["cfr.attempt"] == 2


def test_metrics_use_bounded_labels_only(running, spans, metric_points):
    _, handle = submit(make_request("metrics-1"), release_manifest_ref="spike@0")
    assert handle.get_result() == FINAL_STATE
    _wait_for(spans, "case_run.attempt")

    points = metric_points()
    for name in ("cfr.job.claims", "cfr.queue.claim_wait", "cfr.job.finished", "cfr.stage.outcomes", "cfr.stage.duration"):
        assert name in points, name
    for name, recorded in points.items():
        if name.startswith("cfr."):
            for attributes, _ in recorded:
                assert set(attributes) <= METRIC_LABELS, (name, attributes)
    assert ({"lane": "batch", "outcome": "succeeded"}) in [a for a, _ in points["cfr.job.finished"]]


def test_queue_depth_gauge_reports_unfinished_jobs(runtime_settings, store):
    from cfr_v2.runtime import start_runtime, stop_runtime

    blocker_started = []

    def slow(inp: StageInput) -> StageOutput:
        blocker_started.append(1)
        time.sleep(1.0)
        return placeholder_handler(inp)

    rt = start_runtime(runtime_settings, store, handlers={st.logical_task_id: slow for st in STAGES})
    try:
        submit(make_request("depth-1", lane=Lane.REALTIME), release_manifest_ref="spike@0")
        deadline = time.monotonic() + 10
        while not blocker_started and time.monotonic() < deadline:
            time.sleep(0.05)
        observed = {
            (o.attributes["lane"], o.attributes["status"]): o.value for o in runtime_metrics._observe_queue_depth(None)
        }
        assert observed[("realtime", "running")] == 1
        assert observed[("batch", "queued")] == 0
        assert len(observed) == 4
    finally:
        stop_runtime(workflow_completion_timeout_sec=10)  # drain, so no worker outlives the test
    assert rt.runner is not None


def test_queue_depth_gauge_is_silent_without_lanes(runtime_settings, store):
    from cfr_v2.runtime import start_runtime, stop_runtime

    start_runtime(runtime_settings, store, listen_lanes=())
    try:
        assert runtime_metrics._observe_queue_depth(None) == []
    finally:
        stop_runtime()
    assert runtime_metrics._observe_queue_depth(None) == []  # no runtime at all


def test_intake_batch_traces_and_privacy(running, store, spans, caplog):
    """Spans and logs carry identifiers, counts and outcomes, never case content."""
    caplog.set_level(logging.DEBUG, logger="cfr_v2")  # our logs; botocore's wire-level DEBUG is out of scope
    manifest = ReleaseRegistry(REPO_ROOT / DEFAULT_REGISTRY_DIR).load("cfr-v2-phase-b")
    coordinator = IntakeCoordinator.pinned(
        manifest, store=store, masker=RegexMasker(synthetic_names()), profanity=ProfanityFilter(["frustration"])
    )
    rows = synthetic_rows(3, seed=7)
    report = coordinator.admit_rows(rows)
    run_ids = [r.case_run_id for r in report.results]
    wait_for_state(running.settings.state_database_url, run_ids, FINAL_STATE, timeout=60)
    # Filtered by run: a worker thread left over from an earlier test may still end spans.
    attempts = [a for a in _wait_for(spans, "case_run.attempt", count=3) if a.attributes["cfr.case_run_id"] in run_ids]

    (batch,) = _wait_for(spans, "intake.batch")
    cases = _wait_for(spans, "intake.case", count=3)
    admits = _wait_for(spans, "case_run.admit", count=3)
    assert batch.attributes["cfr.rows"] == 3
    assert all(c.context.trace_id == batch.context.trace_id and c.parent.span_id == batch.context.span_id for c in cases)
    assert {c.attributes["cfr.outcome"] for c in cases} == {"ACCEPTED"}
    assert all(c.attributes["cfr.redaction_spans"] > 0 for c in cases)
    # Each run is its own trace, linked to its intake.case span.
    case_span_ids = {c.context.span_id for c in cases}
    assert {a.links[0].context.span_id for a in admits} == case_span_ids
    assert len({a.context.trace_id for a in admits} | {batch.context.trace_id}) == 4
    assert {a.context.trace_id for a in attempts} == {a.context.trace_id for a in admits}

    # Privacy: no transcript fragment, and no name the masker redacted, anywhere.
    forbidden = set(synthetic_names())
    for row in rows:
        transcript = row["msg_tx2"]
        forbidden |= {transcript[i : i + 24] for i in range(0, max(len(transcript) - 24, 1), 24)}
    exported: list[str] = []
    for s in spans.get_finished_spans():
        exported.append(s.name)
        for key, value in s.attributes.items():
            assert key.startswith("cfr.") and key[4:] in SPAN_ATTRIBUTES, (s.name, key)
            exported.append(str(value))
        for event in s.events:
            exported.extend(str(v) for v in event.attributes.values())
    exported.extend(record.getMessage() for record in caplog.records)
    blob = "\n".join(exported)
    leaked = [text for text in forbidden if text and text in blob]
    assert not leaked, leaked[:3]


# -- Part 2: model calls (LDR-0012) ---------------------------------------------------------

REPLY = "REPLY-MARKER the customer was double charged at the Lisbon hotel"
CLAIM = "CLAIM-MARKER two hotel charges on one stay"
BAD_REPLY = "BADREPLY-MARKER not the schema at all"
CAUSE = "CAUSE-MARKER upstream body echoed the prompt"
TRANSCRIPTS = {  # the tag word picks each case's path through the fake model
    "alpha": "Agent: How can I help? Customer: The hotel charged me twice, alpha booking, please refund one.",
    "bravo": "Agent: Hello there. Customer: My bravo statement shows a fee I never agreed to pay.",
    "charlie": "Agent: Good morning. Customer: The charlie app keeps declining my payment every time.",
}


def _pass1_answer(user: str, tag: str) -> dict:
    transcript = user.split("[[ ## transcript ## ]]\n", 1)[1].split("\n\nRespond", 1)[0]
    item = {"quotes": [transcript[8:40]], "ambiguous": False, "ambiguity_note": None}  # a real excerpt
    text = f"{REPLY} ({tag})"
    return {
        "allegations": [
            item | {"allegation": text, "kind": "CLAIM", "about": "third_party", "details": [], "issue_indicators": []}
        ],
        "requests": [item | {"request": text, "type": "refund_or_credit", "for_allegations": [1]}],
        "agent_statements": [
            item | {"statement": text, "kind": "FACT", "responds_to_allegations": [1], "responds_to_requests": [1]}
        ],
    }


def _pass2_answer() -> dict:
    need = {"description": CLAIM, "source_type": "transactions", "proposition": REPLY}
    return {
        "claims": [
            {
                "allegation_ids": ["a1"],
                "statement": CLAIM,
                "about_an_account": True,
                "account_refs": ["A1"],
                "entities": [{"type": "merchant", "value": CLAIM}],
                "event_window_start": None,
                "event_window_end": None,
                "time_quality": "UNKNOWN",
                "grounding_note": CLAIM,
                "evidence_needs": [need | {"origin": "s1"}, need | {"origin": "r1"}],
            }
        ],
        "other_checks": [],
    }


class _FakeChat:
    """Stands in for safechain's chat model behind the real GatewayModelClient."""

    redaction = None

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ainvoke(self, messages, **kwargs):
        import json
        from types import SimpleNamespace

        user = messages[-1]["content"]
        tag = next(t for t in TRANSCRIPTS if t in user)
        pass1 = "[[ ## transcript ## ]]" in user
        self.calls.append(f"{tag}:{1 if pass1 else 2}")
        if tag == "charlie":
            raise TimeoutError(CAUSE)  # every attempt; the gateway gives up, then the runtime does
        if pass1:
            answer = _pass1_answer(user, tag)
        elif tag == "bravo":
            answer = {"claims": BAD_REPLY, "other_checks": BAD_REPLY}  # JSON, but not the signature
        else:
            answer = _pass2_answer()
        return SimpleNamespace(
            content=json.dumps(answer),
            response_metadata={"finish_reason": "stop"},
            usage_metadata={"input_tokens": 100, "output_tokens": 50},
            additional_kwargs={},
        )


def test_model_calls_keep_prompts_and_replies_out_of_spans_and_logs(running, store, spans, caplog):
    """Prompts and model replies reach artifacts only: never span attributes, events, statuses or logs,
    on success, on unparseable output, or when retries run out and the run fails with a traceback.
    Runs the real C02 handlers and the real gateway client; only the chat model is fake."""
    from cfr_v2.llm import GatewayModelClient, GatewaySettings
    from cfr_v2.llm.dspy_lm import gateway_lm
    from cfr_v2.registry.evidence_sources import load_taxonomy
    from cfr_v2.understanding import make_pass1_handler, make_pass2_handler
    from cfr_v2.understanding.pass1 import ExtractFromCall
    from cfr_v2.understanding.pass2 import INSTRUCTIONS

    caplog.set_level(logging.DEBUG, logger="cfr_v2")
    chat = _FakeChat()

    async def factory(model_key, params):
        return chat

    settings = GatewaySettings(
        model_id="fake-model",
        max_attempts=2,
        backoff_base_sec=0.001,
        backoff_max_sec=0.002,
        attempt_timeout_sec=5.0,
        call_deadline_sec=10.0,
    )
    lm = gateway_lm(GatewayModelClient(settings, model_factory=factory), settings.model_id)
    taxonomy = load_taxonomy("evidence-sources-1", REPO_ROOT / DEFAULT_REGISTRY_DIR)
    running.handlers = {
        **running.handlers,
        "c02.pass1": make_pass1_handler(store, lm),
        "c02.pass2": make_pass2_handler(store, lm, taxonomy),
    }
    rows = [row | {"msg_tx2": text} for row, text in zip(synthetic_rows(3, seed=11), TRANSCRIPTS.values(), strict=True)]
    manifest = ReleaseRegistry(REPO_ROOT / DEFAULT_REGISTRY_DIR).load("cfr-v2-phase-b")
    coordinator = IntakeCoordinator.pinned(
        manifest, store=store, masker=RegexMasker(synthetic_names()), profanity=ProfanityFilter(["frustration"])
    )
    report = coordinator.admit_rows(rows)
    ok, bad_output, failing = (r.case_run_id for r in report.results)
    wait_for_state(running.settings.state_database_url, [ok], FINAL_STATE, timeout=60)
    wait_for_state(running.settings.state_database_url, [bad_output, failing], "FAILED", timeout=60)

    # The paths under test really ran.
    assert {"alpha:1", "alpha:2", "bravo:1", "bravo:2", "charlie:1"} <= set(chat.calls)
    assert any(s.name == "llm.call" for s in spans.get_finished_spans())
    formatter = logging.Formatter()
    logged = [formatter.format(record) for record in caplog.records]  # messages and tracebacks, as written
    assert any("Traceback" in line and "ModelTimeoutError" in line for line in logged)

    forbidden = {REPLY, CLAIM, BAD_REPLY, CAUSE, *synthetic_names()}
    forbidden |= {ExtractFromCall.instructions[:60], INSTRUCTIONS[:60], "[[ ## transcript ## ]]"}  # prompt text
    for text in TRANSCRIPTS.values():
        forbidden |= {text[i : i + 24] for i in range(0, len(text) - 24, 12)}
    exported: list[str] = []
    for s in spans.get_finished_spans():
        exported.append(s.name)
        exported.append(s.status.description or "")
        for key, value in s.attributes.items():
            assert key.startswith("cfr.") and key[4:] in SPAN_ATTRIBUTES, (s.name, key)
            exported.append(str(value))
        for event in s.events:
            exported.extend(str(v) for v in event.attributes.values())
    exported.extend(logged)
    blob = "\n".join(exported)
    leaked = [text for text in forbidden if text and text in blob]
    assert not leaked, leaked[:3]
