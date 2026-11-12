"""Run DSPy's optimizers on one prompt (LDR-0027).

Three methods, all instruction-only, so the result is text a person reviews and commits:

- **GEPA:** reflects on the metric's feedback for failed examples and proposes new instructions;
  the strongest of the three for the calls it spends.
- **MIPROv2 with no demos:** proposes candidate instructions grounded in the data and the program,
  then searches them with Bayesian optimization.
- **COPRO:** coordinate ascent over generated instructions; the cheapest, a baseline.

Every model call goes through ``optimization_lm`` and counts against the run's cap. The runner
scores the baseline and the candidate on the held-out test split with the same metric.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

import dspy

from cfr_v2.llm.dspy_lm import _ADAPTER, CallCapReached, _model_error

from .gold import GoldExample, Score, Split

Method = Literal["gepa", "miprov2", "copro"]
Budget = Literal["light", "medium", "heavy"]
METHODS: tuple[Method, ...] = ("gepa", "miprov2", "copro")
BUDGETS: tuple[Budget, ...] = ("light", "medium", "heavy")
COPRO_SIZE = {"light": (4, 2), "medium": (6, 3), "heavy": (10, 4)}  # breadth, depth

Metric = Callable[[Any, dspy.Prediction], Score]


@dataclass(frozen=True)
class Evaluation:
    scores: dict[str, float]  # per case: its examples' mean score
    errors: int  # examples whose call failed (scored 0)

    @property
    def mean(self) -> float:
        return sum(self.scores.values()) / len(self.scores) if self.scores else 0.0


def instructions_of(program: dspy.Module) -> dict[str, str]:
    return {name: predictor.signature.instructions for name, predictor in program.named_predictors()}


def evaluate(
    program: dspy.Module, examples: Sequence[GoldExample], metric: Metric, lm: dspy.LM, threads: int = 8
) -> Evaluation:
    """Each example once; a failed call scores 0 and is counted. Running out of calls stops it."""

    def one(example: GoldExample) -> tuple[str, float, bool]:
        with dspy.context(lm=lm, adapter=_ADAPTER, disable_history=True):
            try:
                prediction = program(**example.inputs)
            except Exception as exc:  # noqa: BLE001 - a failed example scores 0; the run goes on
                if isinstance(_model_error(exc), CallCapReached):
                    raise CallCapReached(str(_model_error(exc))) from None
                return example.case_id, 0.0, True
        return example.case_id, metric(example.label, prediction).score, False

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(one, examples))
    by_case: dict[str, list[float]] = {}
    for case_id, score, _ in results:
        by_case.setdefault(case_id, []).append(score)
    return Evaluation({c: sum(s) / len(s) for c, s in by_case.items()}, sum(1 for *_, failed in results if failed))


def _dspy_metric(metric: Metric):
    """The metric as DSPy's optimizers call it; GEPA also reads the feedback."""

    def score(example: dspy.Example, prediction: dspy.Prediction, trace=None, pred_name=None, pred_trace=None):
        result = metric(example.gold, prediction)
        return dspy.Prediction(score=result.score, feedback=result.feedback)

    def number(example: dspy.Example, prediction: dspy.Prediction, trace=None):
        return metric(example.gold, prediction).score

    return score, number


def optimize(
    program: dspy.Module,
    data: Split,
    metric: Metric,
    *,
    method: Method,
    budget: Budget,
    lm: dspy.LM,
    threads: int = 8,
) -> dspy.Module:
    """A copy of ``program`` with optimized instructions; ``program`` itself is untouched."""
    feedback, number = _dspy_metric(metric)
    train = [e.to_dspy() for e in data.train]
    val = [e.to_dspy() for e in data.val]
    student = program.deepcopy()
    with dspy.context(lm=lm, adapter=_ADAPTER, disable_history=True):
        match method:
            case "gepa":
                optimizer = dspy.GEPA(
                    metric=feedback,
                    auto=budget,
                    reflection_lm=lm,
                    num_threads=threads,
                    seed=data.seed,
                    track_stats=False,
                )
                return optimizer.compile(student, trainset=train, valset=val)
            case "miprov2":
                optimizer = dspy.MIPROv2(
                    metric=number,
                    auto=budget,
                    max_bootstrapped_demos=0,  # instructions only (decision 3)
                    max_labeled_demos=0,
                    num_threads=threads,
                    seed=data.seed,
                    verbose=False,
                )
                return optimizer.compile(student, trainset=train, valset=val)
            case "copro":
                breadth, depth = COPRO_SIZE[budget]
                optimizer = dspy.COPRO(metric=number, breadth=breadth, depth=depth, init_temperature=1.0)
                return optimizer.compile(
                    student, trainset=train + val, eval_kwargs={"num_threads": threads, "display_progress": False}
                )
    raise ValueError(f"unknown method {method!r}")


class Progress:
    """Prints ``optimize: [used/cap] model calls; about … left`` at most every ``every`` seconds,
    the line the console reads as a progress bar. "Left" is the time to spend the whole cap at the
    current pace: an upper bound, since a run usually finishes sooner."""

    def __init__(self, cap: int, *, every: float = 10.0, out=print) -> None:
        self.cap, self.every, self.out = cap, every, out
        self.started = time.monotonic()
        self.last = 0.0

    def __call__(self, used: int) -> None:
        now = time.monotonic()
        if now - self.last < self.every and used < self.cap:
            return
        self.last = now
        pace = (now - self.started) / used
        self.out(f"optimize: [{used}/{self.cap}] model calls; about {_duration(pace * (self.cap - used))} left at most")


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


__all__ = ["BUDGETS", "METHODS", "Evaluation", "Progress", "evaluate", "instructions_of", "optimize"]
