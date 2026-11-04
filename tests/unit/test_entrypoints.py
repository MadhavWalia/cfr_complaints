"""Module entry points: everything a ``python -m`` run needs must be defined before it starts."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


def _is_main_guard(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and "__name__" in ast.dump(node.test) and "__main__" in ast.dump(node.test)


ENTRY_POINTS = sorted(p for p in SRC.rglob("*.py") if any(_is_main_guard(n) for n in ast.parse(p.read_text()).body))


def test_entry_points_were_found():
    assert len(ENTRY_POINTS) >= 8


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: str(p.relative_to(SRC)))
def test_main_guard_is_the_last_statement(path):
    """Code after the guard runs only on import: ``python -m`` exits first and it is never defined."""
    assert _is_main_guard(ast.parse(path.read_text()).body[-1])
