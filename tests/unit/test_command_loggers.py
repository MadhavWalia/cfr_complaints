"""Commands run with ``python -m`` are named ``__main__``: a logger named after the module would sit
outside the ``cfr_v2`` hierarchy, so our logging setup would never print it. Its INFO lines were only
seen by accident, through a root handler a dependency added, and vanished when that was removed."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "cfr_v2"


@pytest.mark.parametrize("path", sorted(SRC.rglob("__main__.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_command_loggers_are_named_inside_cfr_v2(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) in (
            "get_logger",
            "getLogger",
        ):
            [arg] = node.args[:1] or [None]
            assert not (isinstance(arg, ast.Name) and arg.id == "__name__"), f"{path.name}: logger named __name__"
            if isinstance(arg, ast.Constant):
                assert str(arg.value).startswith("cfr_v2."), arg.value
