"""One optimization run, end to end (LDR-0027): split, baseline on test, optimize, candidate on
test, report; and, only when the candidate is better on test, its instructions and the patch.

Everything it writes goes to ``out``: ``report.json`` (case ids, scores, instructions; no case
text), and ``instructions.txt`` and ``patch.diff`` when there is something to review.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cfr_v2.llm import ModelClient
from cfr_v2.llm.dspy_lm import CallBudget, CallCapReached, ProgramCall, _model_error, optimization_lm

from .gold import GoldSet, GoldSpec, dataset_hash, split
from .prompts import Prompt
from .report import Report, patch
from .runner import Budget, Method, Progress, evaluate, instructions_of, optimize


@contextmanager
def quiet_dspy():
    """DSPy logs a failed example whole (inputs and gold label) and its optimizers log their data;
    none of it may reach the job log or telemetry. The run prints its own counts instead."""
    logger = logging.getLogger("dspy")
    level, propagate = logger.level, logger.propagate
    logger.setLevel(logging.CRITICAL + 1)
    logger.propagate = False
    try:
        yield
    finally:
        logger.setLevel(level)
        logger.propagate = propagate


def run(
    spec: GoldSpec,
    prompt: Prompt,
    gold: GoldSet,
    ctx: Any,
    *,
    client: ModelClient,
    model_id: str,
    method: Method,
    budget: Budget,
    cap: int,
    seed: int,
    threads: int,
    out: Path,
    say=print,
) -> Report:
    data = split(gold.examples, seed)
    calls = CallBudget(cap)
    progress = Progress(cap, out=say)
    version = prompt.current_version()
    call = ProgramCall(f"optimize.{prompt.prompt_id}", version, "optimization", f"{method}-{seed}", redacted=True)
    lm = optimization_lm(client, model_id, call=call, budget=calls, on_call=progress)
    program = spec.program(ctx)
    report = Report(
        prompt_id=prompt.prompt_id,
        program_version=version,
        method=method,
        budget=budget,
        seed=seed,
        model_id=model_id,
        dataset=dataset_hash(gold.examples),
        sizes=dict(zip(("train", "val", "test"), data.sizes, strict=True)),
        calls_cap=cap,
        instructions_before=instructions_of(program)[prompt.predictor],
    )
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with quiet_dspy():
        try:
            say(f"optimize: baseline on {len(data.test)} test examples")
            base = evaluate(program, data.test, spec.metric, lm, threads)
            report.baseline, report.errors["baseline"] = base.scores, base.errors
            say(
                f"optimize: {method}, {budget} budget, on {len(data.train)} train and {len(data.val)} validation examples"
            )
            candidate = optimize(program, data, spec.metric, method=method, budget=budget, lm=lm, threads=threads)
            report.instructions_after = instructions_of(candidate)[prompt.predictor]
            if calls.used >= cap:
                report.stopped = (
                    f"the cap of {cap} model calls was reached while optimizing; the candidate wasn't scored"
                )
            else:
                say(f"optimize: candidate on {len(data.test)} test examples")
                scored = evaluate(candidate, data.test, spec.metric, lm, threads)
                report.candidate, report.errors["candidate"] = scored.scores, scored.errors
        except Exception as exc:  # noqa: BLE001 - the report says how far it got
            found = _model_error(exc) or exc
            report.stopped = (
                f"the cap of {cap} model calls was reached"
                if isinstance(found, CallCapReached)
                else f"{type(found).__name__}: {str(found)[:200]}"
            )
        finally:
            report.calls_used = calls.used
            report.seconds = time.monotonic() - started
            (out / "report.json").write_text(report.to_json(), encoding="utf-8")
    if report.better:
        (out / "instructions.txt").write_text(report.instructions_after.strip() + "\n", encoding="utf-8")
        (out / "patch.diff").write_text(patch(prompt, report.instructions_after), encoding="utf-8")
    for line in report.lines():
        say(line)
    return report


def baseline(
    spec: GoldSpec, prompt: Prompt, gold: GoldSet, ctx: Any, *, client, model_id, cap, seed, threads, say=print
) -> tuple[float, float]:
    """The current prompt's mean score on the validation and test splits."""
    data = split(gold.examples, seed)
    call = ProgramCall(f"optimize.{prompt.prompt_id}", prompt.current_version(), "optimization", "baseline", True)
    calls = CallBudget(cap)
    lm = optimization_lm(client, model_id, call=call, budget=calls, on_call=Progress(cap, out=say))
    program = spec.program(ctx)
    with quiet_dspy():
        val = evaluate(program, data.val, spec.metric, lm, threads)
        test = evaluate(program, data.test, spec.metric, lm, threads)
    say(f"baseline: validation {val.mean:.3f} ({len(data.val)} examples, {val.errors} failed calls)")
    say(f"baseline: test {test.mean:.3f} ({len(data.test)} examples, {test.errors} failed calls)")
    say(f"model calls: {calls.used} of {cap}")
    return val.mean, test.mean


__all__ = ["baseline", "run"]
