from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from cfr_v2.contracts import canonical_hash, canonical_json


def test_key_order_does_not_change_hash():
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})


def test_list_order_does_change_hash():
    assert canonical_hash([1, 2]) != canonical_hash([2, 1])


def test_sets_are_order_insensitive():
    assert canonical_hash({"s": {3, 1, 2}}) == canonical_hash({"s": {1, 2, 3}})


def test_datetimes_normalized_to_utc():
    utc = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ist = utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert canonical_hash({"t": utc}) == canonical_hash({"t": ist})


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="naive"):
        canonical_json({"t": datetime(2026, 1, 1)})


def test_nan_rejected():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_unicode_is_stable():
    assert canonical_json({"k": "café"}) == '{"k":"café"}'.encode()


def test_hash_stable_across_processes():
    code = "from cfr_v2.contracts import canonical_hash; print(canonical_hash({'b': 2, 'a': [1, {'z': None}]}))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == canonical_hash({"a": [1, {"z": None}], "b": 2})
