"""Prompt optimization (LDR-0027 phase 1): the prompts, the split, the optimization LM, the three
optimizers end to end on a toy prompt, the report and the patch."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import textwrap

import dspy
import pytest

from cfr_v2.llm import FakeModelClient, dspy_lm
from cfr_v2.optimize import __main__ as cli
from cfr_v2.optimize import session
from cfr_v2.optimize.gold import GoldExample, GoldSet, NotEnoughExamples, Score, dataset_hash, split, warnings
from cfr_v2.optimize.prompts import PROMPTS, Prompt
from cfr_v2.optimize.report import Report, next_version, paired_difference, patched

HINT = "Answer in UPPERCASE."

# --- the toy prompt: correct only when its instructions ask for capitals ------------------------

TOY = '''
import dspy

TOY_VERSION = "toy-1"


class Echo(dspy.Signature):
    """Repeat the word."""

    word: str = dspy.InputField()
    answer: str = dspy.OutputField()


class Toy(dspy.Module):
    def __init__(self):
        super().__init__()
        self.say = dspy.Predict(Echo)

    def forward(self, word):
        return self.say(word=word)
'''


@pytest.fixture
def toy(tmp_path, monkeypatch):
    path = tmp_path / "toy_prompt.py"
    path.write_text(textwrap.dedent(TOY))
    spec = importlib.util.spec_from_file_location("toy_prompt", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "toy_prompt", module)
    spec.loader.exec_module(module)
    return Prompt("toy", "Toy", "Repeats a word.", "toy_prompt", "say", "doc:Echo", "TOY_VERSION", source=path), module


class ToySpec:
    prompt_id = "toy"

    def __init__(self, module):
        self.module = module

    def program(self, ctx):
        return self.module.Toy()

    def metric(self, label, prediction):
        ok = (prediction.answer or "").strip() == label
        return Score(1.0 if ok else 0.0, "" if ok else "The answer must be the word in capital letters.")


def _fill(schema):
    if "anyOf" in schema:
        return _fill(schema["anyOf"][0])
    kind = schema.get("type")
    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return 0
    if kind == "object":
        return {k: _fill(v) for k, v in schema.get("properties", {}).items()}
    return HINT


def responder(request):
    """Any optimizer's own calls get HINT as their text; the toy's answer follows its instructions."""
    if request.output_schema is None:
        return {"text": f"```\n{HINT}\n```"}
    props = request.output_schema.get("properties", {})
    answer = {k: _fill(v) for k, v in props.items()}
    if "answer" in props and "word" in request.messages[-1].content:
        word = re.findall(r"[a-z]+", request.messages[-1].content.split("word")[-1])[0]
        answer["answer"] = word.upper() if "UPPERCASE" in request.instructions else word
    return answer


WORDS = ["apple", "river", "stone", "cloud", "maple", "tiger", "lemon", "ocean", "piano", "grape", "eagle", "honey", "jelly", "kiwi", "mango", "otter", "pearl", "quilt", "robin", "salt", "tulip", "umbra", "viola", "wheat"]  # fmt: skip
GOLD = GoldSet([GoldExample(f"case-{i}", {"word": w}, w.upper()) for i, w in enumerate(WORDS)])


@pytest.mark.parametrize("method", ["copro", "miprov2", "gepa"])
def test_each_method_finds_better_instructions_and_offers_the_patch(toy, tmp_path, method):
    prompt, module = toy
    client = FakeModelClient(responder=responder)
    lines: list[str] = []
    report = session.run(ToySpec(module), prompt, GOLD, None, client=client, model_id="fake", method=method,
                         budget="light", cap=3000, seed=1, threads=2, out=tmp_path / "run", say=lines.append)  # fmt: skip
    assert report.stopped is None, report.stopped
    assert sum(report.baseline.values()) == 0 and all(v == 1.0 for v in report.candidate.values())
    assert "UPPERCASE" in report.instructions_after and report.better
    assert {r.program_id for r in client.requests} == {"optimize.toy"}  # one identity, through the client
    saved = json.loads((tmp_path / "run" / "report.json").read_text())
    assert saved["summary"]["better"] and saved["calls_used"] == report.calls_used <= 3000
    text = (tmp_path / "run" / "report.json").read_text().lower()
    assert not any(word in text for word in WORDS if len(word) > 4)  # case ids and scores, no case text
    diff = (tmp_path / "run" / "patch.diff").read_text()
    assert '+TOY_VERSION = "toy-2"' in diff and HINT in diff
    assert any(line.startswith("result: the candidate is better on test") for line in lines)


def test_the_call_cap_stops_a_run_and_the_report_says_so(toy, tmp_path):
    prompt, module = toy
    report = session.run(ToySpec(module), prompt, GOLD, None, client=FakeModelClient(responder=responder),
                         model_id="fake", method="gepa", budget="light", cap=10, seed=1, threads=2,
                         out=tmp_path / "run", say=lambda line: None)  # fmt: skip
    assert report.calls_used == 10 and "cap of 10 model calls" in (report.stopped or "")
    assert not report.better and not (tmp_path / "run" / "patch.diff").exists()
    assert (tmp_path / "run" / "report.json").is_file()


def test_the_optimization_lm_is_redacted_capped_and_identified():
    client = FakeModelClient(responder=lambda request: {"answer": "blue"})
    budget = dspy_lm.CallBudget(1)
    call = dspy_lm.ProgramCall("optimize.x", "v1", "optimization", "t", redacted=True)
    lm = dspy_lm.optimization_lm(client, "m", call=call, budget=budget)
    with dspy.context(lm=lm, adapter=dspy_lm._ADAPTER):
        assert dspy.Predict("question -> answer")(question="sky?").answer == "blue"
        with pytest.raises(Exception, match="cap of 1 model calls"):
            dspy.Predict("question -> answer")(question="again?")
    assert client.requests[0].program_id == "optimize.x" and client.requests[0].redacted
    with pytest.raises(dspy_lm.UnredactedPayloadError):
        dspy_lm.optimization_lm(client, "m", call=dspy_lm.ProgramCall("x", "v", "c", "t", False), budget=budget)


# --- the split, the report, the patch -----------------------------------------------------------


def test_the_split_is_by_case_deterministic_and_refuses_too_few():
    examples = [GoldExample(f"c{i // 2}", {"x": str(i)}, i) for i in range(60)]  # two examples per case
    first, again = split(examples, 7), split(list(reversed(examples)), 7)
    assert first.sizes == (30, 16, 14) and {e.case_id for e in first.test} == {e.case_id for e in again.test}
    sides = [{e.case_id for e in part} for part in (first.train, first.val, first.test)]
    assert not (sides[0] & sides[1] or sides[0] & sides[2] or sides[1] & sides[2])
    assert {e.case_id for e in split(examples, 8).test} != sides[2]
    with pytest.raises(NotEnoughExamples, match="19 usable examples; at least 20"):
        split(examples[:19], 1)
    assert warnings(examples[:30]) and not warnings(examples)
    assert dataset_hash(examples) == dataset_hash(list(reversed(examples))) != dataset_hash(examples[1:])


def test_the_difference_and_its_interval():
    before = {f"c{i}": 0.5 for i in range(40)}
    assert paired_difference(before, {c: 0.8 for c in before})[1] > 0.29  # a clear gain
    mixed = {c: (0.9 if i % 2 else 0.1) for i, c in enumerate(before)}
    mean, low, high = paired_difference(before, mixed)
    assert abs(mean) < 1e-9 and low < 0 < high  # no gain: the interval spans zero
    report = Report("x", "v-1", "gepa", "light", 1, "m", "sha256:" + "0" * 64, {"test": 40}, 100,
                    baseline=before, candidate=mixed, instructions_before="a", instructions_after="b")  # fmt: skip
    assert not report.better and report.changed()[0] and report.changed()[1]
    assert Report.from_json(report.to_json()).candidate == mixed


def test_a_patch_puts_the_instructions_in_and_bumps_the_version(tmp_path):
    assert [next_version(v) for v in ("pass1-2", "route-9", "select-10")] == ["pass1-3", "route-10", "select-11"]
    new = "Do the task well.\n\nA second paragraph."
    for prompt in PROMPTS:
        path = tmp_path / f"{prompt.prompt_id}.py"
        path.write_text(patched(prompt, new))
        name = prompt.module.rsplit(".", 1)[0] + "._patched_test"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            del sys.modules[name]
        kind = prompt.instructions
        got = getattr(module, kind[4:]).instructions if kind.startswith("doc:") else module.INSTRUCTIONS.strip()
        assert got == new, prompt.prompt_id
        assert getattr(module, prompt.version_constant) == next_version(prompt.current_version())
    with pytest.raises(ValueError, match='contain """'):
        patched(PROMPTS[0], 'Say """ hi')


# --- the command ------------------------------------------------------------------------------


def test_the_prompts_are_listed_and_the_rest_waits_for_their_gold_specs(capsys):
    assert cli.main(["prompts"]) == 0
    out = capsys.readouterr().out
    for prompt in PROMPTS:
        assert prompt.prompt_id in out and prompt.current_version() in out
    assert "no gold spec yet (phase 2)" in out
    with pytest.raises(SystemExit) as stopped:
        cli.main(["check", "c02.pass1", "gold.xlsx"])
    assert stopped.value.code == 2 and "arrives in phase 2" in capsys.readouterr().err


def test_failed_examples_never_reach_the_logs(toy, tmp_path, caplog, capfd):
    """DSPy logs a failed example whole (inputs and gold label); a run keeps that out of every log."""
    prompt, module = toy

    def flaky(request):
        if "pearl" in request.messages[-1].content:
            raise RuntimeError("gateway hiccup")
        return responder(request)

    caplog.set_level("DEBUG")
    session.run(ToySpec(module), prompt, GOLD, None, client=FakeModelClient(responder=flaky), model_id="fake",
                method="gepa", budget="light", cap=3000, seed=1, threads=2, out=tmp_path / "run", say=lambda line: None)  # fmt: skip
    seen = caplog.text.lower() + "".join(capfd.readouterr()).lower()
    assert "pearl" not in seen and "otter" not in seen


def test_each_pipeline_program_exposes_its_instructions_and_keeps_its_prompt():
    """One named predictor per program holds the editable instructions; per-call signatures add the
    taxonomy, claim ids or verticals around them, giving exactly the prompt the stage sent before."""
    from cfr_v2.registry.evidence_sources import load_taxonomy
    from cfr_v2.routing import router
    from cfr_v2.selection import selector
    from cfr_v2.understanding import pass1, pass2
    from tests.unit.routing.test_router import CATALOG

    taxonomy = load_taxonomy()
    programs = {
        "c02.pass1": (pass1.Pass1(), None),
        "c02.pass2": (pass2.Pass2(taxonomy), lambda p: (p.signature(), f"{pass2.INSTRUCTIONS}\n\n{pass2.render_source_types(taxonomy)}")),
        "c03.primary_selection": (selector.Selector(), lambda p: (p.signature(["c1", "c2"]), selector.INSTRUCTIONS)),
        "c04.routing": (router.Router(CATALOG), lambda p: (p.signature(["S1"]), router.INSTRUCTIONS + CATALOG.text)),
    }  # fmt: skip
    for prompt in PROMPTS:
        program, built = programs[prompt.prompt_id]
        names = [name for name, _ in program.named_predictors()]
        assert names == [prompt.predictor], prompt.prompt_id
        assert program.named_predictors()[0][1].signature.instructions == prompt.current_instructions()
        if built is not None:
            signature, before = built(program)
            assert signature.instructions == dspy.Signature({"x": (str, dspy.InputField())}, before).instructions
