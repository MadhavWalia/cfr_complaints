"""Retiring and restoring case runs (LDR-0045 step 3): who changes, why the rest don't, the CLI."""

from __future__ import annotations

import pytest
import sqlglot

from cfr_v2.runtime import retire
from cfr_v2.runtime.retire import Found, decide, summary


def test_retire_skips_runs_queued_running_already_retired_or_unknown():
    found = [
        Found("done", "SUCCEEDED", False),
        Found("failed", "FAILED", False),
        Found("busy", "RUNNING", False),
        Found("waiting", "QUEUED", False),
        Found("gone", None, True),
    ]
    decided = decide("retire", ["done", "failed", "busy", "waiting", "gone", "nope", "done"], found)
    assert decided["changed"] == ["done", "failed"]
    assert decided["queued or running: cancel it first"] == ["busy", "waiting"]
    assert decided["already retired"] == ["gone"] and decided["not a case run here"] == ["nope"]
    assert summary("retire", decided)[-1] == "2 runs retired"


def test_restore_changes_only_retired_runs():
    decided = decide("restore", ["a", "b"], [Found("a", "SUCCEEDED", True), Found("b", "SUCCEEDED", False)])
    assert decided == {"changed": ["a"], "not retired": ["b"]}
    assert summary("restore", decided) == ["1 skipped: not retired", "1 runs restored"]


@pytest.mark.parametrize("name", ["STATES_SQL", "INSERT_SQL"])
def test_the_queries_are_postgres(name):
    assert sqlglot.parse_one(getattr(retire, name).replace("%(", "__").replace(")s", "__"), read="postgres")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["restore", "--reason", "why"], "--reason goes with retire"),
        (["retire", "--reason", "x" * 201], "at most 200 characters"),
    ],
)
def test_the_cli_refuses_before_touching_anything(tmp_path, capsys, extra, message):
    runs = tmp_path / "runs.txt"
    runs.write_text("run-1\n")
    with pytest.raises(SystemExit) as exit_:
        retire.main([extra[0], "--runs-file", str(runs), *extra[1:]])
    assert exit_.value.code == 2 and message in capsys.readouterr().err
    runs.write_text("\n# nothing\n")
    with pytest.raises(SystemExit):
        retire.main(["retire", "--runs-file", str(runs)])
    assert "no case run ids" in capsys.readouterr().err
