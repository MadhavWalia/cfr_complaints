"""Re-running case runs from a stage (LDR-0045): what is copied, who qualifies, and the CLI's refusals."""

from __future__ import annotations

import pytest
import sqlglot

from cfr_v2.runtime import rerun
from cfr_v2.runtime.rerun import Result, Source, copy_chain, plan, rerun_id, stage_for, summary
from cfr_v2.runtime.stages import STAGES
from cfr_v2.runtime.workflow import stage_input_hash

REQUEST = "sha256:" + "1" * 64
MANIFEST = "cfr-v2-phase-f@1c10023458ee"


def _results(request: str = REQUEST, *, upto: int = len(STAGES), versions: dict[str, str] | None = None):
    """A run's committed results, chained as the engine chains them."""
    found, upstream = [], []
    for i, stage in enumerate(STAGES[:upto]):
        ref = f"sha256:{i:064d}"
        version = (versions or {}).get(stage.logical_task_id, stage.program_version)
        found.append(
            Result(
                stage.logical_task_id,
                stage_input_hash(request, stage.logical_task_id, tuple(upstream)),
                version,
                "SUCCEEDED",
                (ref,),
            )
        )
        upstream.append(ref)
    return found


def _source(run="run-1", state="PLAN_READY", job="SUCCEEDED"):
    return Source(run, "GC-1", "case-1", 0, "batch", REQUEST, "cfr-v2-phase-e", state, job)


def test_a_stage_is_named_by_id_or_short_name():
    assert stage_for("routing") == stage_for("c04.routing") == (3, STAGES[3])
    assert stage_for("Pass1")[1].logical_task_id == "c02.pass1"
    with pytest.raises(rerun.RerunRefused, match="no stage 'intake'"):
        stage_for("intake")


@pytest.mark.parametrize(
    ("start", "copied"), [(0, []), (1, ["c02.pass1"]), (3, ["c02.pass1", "c02.pass2", "c03.primary_selection"])]
)
def test_the_stages_before_the_start_are_copied_in_order(start, copied):
    chain = copy_chain(REQUEST, _results(), start)
    assert [r.logical_task_id for r in chain] == copied


def test_the_copied_results_have_the_input_hashes_the_engine_will_compute():
    chain = copy_chain(REQUEST, _results(), 3)
    upstream = []
    for result, stage in zip(chain, STAGES, strict=False):
        assert result.input_hash == stage_input_hash(REQUEST, stage.logical_task_id, tuple(upstream))
        upstream.append(result.output_artifact_refs[0])


def test_an_earlier_stage_from_an_older_version_is_not_reused():
    old = _results(versions={"c02.pass1": "pass1-1"})
    assert copy_chain(REQUEST, old, 3) == f"c02.pass1 is pass1-1, this code runs {STAGES[0].program_version}"
    assert copy_chain(REQUEST, old, 0) == []  # from Pass 1, nothing is reused: any run qualifies


def test_an_earlier_stage_that_never_succeeded_or_does_not_chain_is_not_reused():
    assert copy_chain(REQUEST, _results(upto=2), 3) == "c03.primary_selection never succeeded"
    other = _results("sha256:" + "2" * 64)  # results computed for another request
    assert "doesn't follow" in copy_chain(REQUEST, other, 2)


def test_who_qualifies_and_why_the_rest_are_skipped():
    sources = [
        _source("ok"),
        _source("busy", job="QUEUED"),
        _source("failed", state="FAILED", job="FAILED"),
        _source("cancelled", state="CANCELLED"),
        _source("done-before"),
        _source("old"),
    ]
    results = [(s, r) for s in ("ok", "failed", "done-before") for r in _results()]
    results += [("old", r) for r in _results(versions={"c02.pass2": "pass2-0"})]
    planned = {
        p.source.case_run_id: p
        for p in plan(sources, results, {"done-before"}, start=3, manifest_ref=MANIFEST, include_failed=False)
    }
    assert planned["ok"].status == "rerun" and planned["ok"].rerun_id == rerun_id("ok", MANIFEST, "c04.routing")
    assert [r.logical_task_id for r in planned["ok"].chain] == ["c02.pass1", "c02.pass2", "c03.primary_selection"]
    assert (planned["busy"].status, planned["busy"].reason) == ("skipped", "queued or running")
    assert planned["failed"].reason == "failed" and "not Plan ready (cancelled)" == planned["cancelled"].reason
    assert planned["done-before"].status == "exists"
    assert planned["old"].reason.startswith("c02.pass2 is pass2-0")
    lines = summary(list(planned.values()), STAGES[3], "cfr-v2-phase-f")
    assert lines[0] == "1 runs to re-run from c04.routing under cfr-v2-phase-f" and len(lines) == 6

    with_failed = plan(sources, results, set(), start=3, manifest_ref=MANIFEST, include_failed=True)
    assert {p.source.case_run_id for p in with_failed if p.status == "rerun"} == {"ok", "failed", "done-before"}


def test_a_rerun_id_is_the_same_for_the_same_source_release_and_stage_only():
    a = rerun_id("run-1", MANIFEST, "c04.routing")
    assert a == rerun_id("run-1", MANIFEST, "c04.routing")
    assert (
        len(
            {
                a,
                rerun_id("run-2", MANIFEST, "c04.routing"),
                rerun_id("run-1", "other@1", "c04.routing"),
                rerun_id("run-1", MANIFEST, "c02.pass2"),
            }
        )
        == 4
    )


@pytest.mark.parametrize("name", ["SOURCES_SQL", "RESULTS_SQL", "EXISTING_SQL"])
def test_the_queries_are_postgres(name):
    sql = getattr(rerun, name).replace("%(", "__").replace(")s", "__")
    assert sqlglot.parse_one(sql, read="postgres") is not None


def test_given_ids_are_taken_as_given_and_a_release_gives_its_latest_run_per_case():
    sql = rerun.SOURCES_SQL
    given, per_case = sql.split("UNION ALL")
    assert "DISTINCT ON" not in given and "DISTINCT ON (c.source_case_id)" in per_case


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--stage", "intake", "--from-release", "r"], "no stage 'intake'"),
        (["--stage", "routing", "--from-release", "r", "--sample", "5", "--case-no", "A"], "not both"),
        (["--stage", "routing", "--from-release", "r", "--sample", "0"], "at least 1"),
    ],
)
def test_the_cli_refuses_before_touching_anything(argv, message, capsys):
    with pytest.raises(SystemExit) as exit_:
        rerun.main(argv)
    assert exit_.value.code == 2 and message in capsys.readouterr().err


def test_ids_and_a_sample_do_not_mix_and_an_empty_file_is_refused(tmp_path, capsys):
    ids = tmp_path / "runs.txt"
    ids.write_text("# none\n\n")
    with pytest.raises(SystemExit):
        rerun.main(["--stage", "routing", "--runs-file", str(ids)])
    assert "no case run ids" in capsys.readouterr().err
    ids.write_text("run-1\n")
    with pytest.raises(SystemExit):
        rerun.main(["--stage", "routing", "--runs-file", str(ids), "--sample", "3"])
    assert "go with --from-release" in capsys.readouterr().err


def test_a_release_that_does_not_match_the_code_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("CFR_RELEASE_ID", "cfr-v2-phase-e")  # route-1: this code runs route-2
    with pytest.raises(SystemExit) as exit_:
        rerun.main(["--stage", "routing", "--from-release", "cfr-v2-phase-e"])
    assert exit_.value.code == 2 and "c04.routing: code runs route-2" in capsys.readouterr().err
