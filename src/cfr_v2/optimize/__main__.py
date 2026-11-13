"""Optimize a pipeline prompt with DSPy, against a gold set (LDR-0027).

    python -m cfr_v2.optimize prompts
    python -m cfr_v2.optimize template c03.primary_selection --case-no GC-1 --case-no GC-2 --out gold.xlsx
    python -m cfr_v2.optimize check c03.primary_selection gold_AB.xlsx gold_CD.xlsx
    python -m cfr_v2.optimize baseline c03.primary_selection gold_*.xlsx
    python -m cfr_v2.optimize run c03.primary_selection gold_*.xlsx --method gepa --budget light --cap 500 --out run1
    python -m cfr_v2.optimize report run1/report.json

``run`` scores the current prompt on a held-out test split, optimizes its instructions on the
train and validation splits (GEPA, MIPROv2 without demos, or COPRO), scores the candidate on the
same test split and writes ``report.json``; when the candidate is better on test, also its
instructions and ``patch.diff`` to review, commit, and release (LDR-0022). Every model call goes
through the gateway, counts against ``--cap`` and is shown as ``[n/N]`` progress.

**Gold workbooks hold case text: keep them, and the run's folder, on this machine, and delete
them once reviewed.** Printed output holds ids, counts and scores only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .context import Context
from .gold import NotEnoughExamples, split, warnings
from .prompts import BY_ID, PROMPTS
from .report import Report
from .runner import BUDGETS, METHODS
from .specs import SPECS


def _spec(prompt_id: str):
    prompt = BY_ID.get(prompt_id)
    if prompt is None:
        raise SystemExit(f"error: no prompt {prompt_id!r}; see `python -m cfr_v2.optimize prompts`")
    spec = SPECS.get(prompt_id)
    if spec is None:
        print(f"error: {prompt.title} has no gold spec yet (LDR-0027)", file=sys.stderr)
        raise SystemExit(2)
    return prompt, spec


def _cases(args) -> list[str]:
    cases = list(args.case_no or [])
    if args.cases_file:
        cases += [line.strip() for line in args.cases_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    return list(dict.fromkeys(cases))


def _gold(spec, paths: list[Path], ctx):
    gold = spec.read(paths, ctx)
    for problem in gold.problems:
        print(f"problem: {problem}")
    return gold


def _client():
    from cfr_v2.llm import GatewayModelClient, GatewaySettings

    settings = GatewaySettings.from_env()
    return GatewayModelClient(settings), settings.model_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.optimize", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prompts", help="the pipeline's prompts, their versions and whether they can be optimized")
    step = sub.add_parser("template", help="a gold workbook to fill, for the cases given")
    step.add_argument("prompt")
    step.add_argument("--case-no", action="append", help="a case number (repeat)")
    step.add_argument("--cases-file", type=Path, help="a text file of case numbers, one per line")
    step.add_argument("--out", type=Path, required=True)
    for name, text in (("check", "read filled workbooks: examples, problems and the split"),
                       ("baseline", "score the current prompt on the validation and test splits"),
                       ("run", "optimize the prompt's instructions and compare on the test split")):  # fmt: skip
        step = sub.add_parser(name, help=text)
        step.add_argument("prompt")
        step.add_argument("gold", type=Path, nargs="+", help="filled gold workbooks")
        step.add_argument("--seed", type=int, default=1, help="the split's (and the optimizer's) seed")
        if name in ("baseline", "run"):
            step.add_argument("--cap", type=int, default=500, help="model calls at most (default 500)")
            step.add_argument("--threads", type=int, default=8, help="examples at once (default 8)")
        if name == "run":
            step.add_argument("--method", choices=METHODS, default="gepa")
            step.add_argument("--budget", choices=BUDGETS, default="light")
            step.add_argument("--out", type=Path, required=True, help="folder for report.json and the patch")
    step = sub.add_parser("report", help="print a run's report")
    step.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.command == "prompts":
        for prompt in PROMPTS:
            state = "ready" if prompt.prompt_id in SPECS else "no gold spec yet"
            print(f"{prompt.prompt_id:24} {prompt.current_version():10} {prompt.title}: {state}")
        return 0
    if args.command == "report":
        for line in Report.from_json(args.path.read_text(encoding="utf-8")).lines():
            print(line)
        return 0

    prompt, spec = _spec(args.prompt)
    ctx = Context()
    try:
        return _run(args, parser, prompt, spec, ctx)
    finally:
        ctx.close()


def _run(args, parser, prompt, spec, ctx) -> int:
    if args.command == "template":
        cases = _cases(args)
        if not cases:
            parser.error("give --case-no or --cases-file")
        for line in spec.template(cases, args.out, ctx):
            print(f"not included: {line}")
        print(f"wrote {args.out}")
        return 0

    gold = _gold(spec, args.gold, ctx)
    try:
        data = split(gold.examples, args.seed)
    except NotEnoughExamples as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in warnings(gold.examples):
        print(f"warning: {warning}")
    print(f"{len(gold.examples)} examples: {data.sizes[0]} train, {data.sizes[1]} validation, {data.sizes[2]} test")
    if args.command == "check":
        print(f"{len(gold.problems)} problems" if gold.problems else "no problems")
        return 0  # enough examples to go on; the problems above are left out and listed

    from cfr_v2.llm import close_model_loop
    from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

    from . import session

    init_telemetry(service_name="cfr-v2-optimize")
    client, model_id = _client()
    try:
        if args.command == "baseline":
            session.baseline(spec, prompt, gold, ctx, client=client, model_id=model_id, cap=args.cap,
                             seed=args.seed, threads=args.threads)  # fmt: skip
            return 0
        report = session.run(spec, prompt, gold, ctx, client=client, model_id=model_id, method=args.method,
                             budget=args.budget, cap=args.cap, seed=args.seed, threads=args.threads, out=args.out)  # fmt: skip
        return 0 if report.candidate and not report.stopped else 1
    finally:
        close_model_loop()
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
