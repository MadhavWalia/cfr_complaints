"""The round-trip fixture for the stage tests."""

from __future__ import annotations

import pytest

from tests.unit.evaluation.fakes import RoundTrip


@pytest.fixture
def roundtrip(tmp_path):
    return lambda stage: RoundTrip(tmp_path, stage)
