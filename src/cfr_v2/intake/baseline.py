"""LUMI baseline from the intake query's baseline columns (LDR-0014).

The combined case query returns, per case, ``baseline_llm_context`` (JSON of the
approved allowlist), ``baseline_ref_map`` (``account_ref`` -> ``rel_id``, ``cm15``) and
``baseline_n_accounts``. They become a ``BaselineProfile`` (INTERNAL, the only part a
model may see) and a ``BaselineRefMap`` (RESTRICTED). Anything that does not fit the
contracts, including a field outside the allowlist, makes the baseline ``invalid``
rather than reaching a model. Failures never carry field values.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from cfr_v2.contracts import BaselineProfile, BaselineRefMap

from .source import BASELINE_OBSERVED_AT_COLUMN


class BaselineStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"  # no cm15 on the case, or no C360 match
    INVALID = "invalid"  # returned but unusable; nothing stored
    NOT_QUERIED = "not_queried"  # the source ran without the baseline


@dataclass(frozen=True)
class ParsedBaseline:
    status: BaselineStatus
    observed_at: datetime | None = None
    profile: BaselineProfile | None = None
    ref_map: BaselineRefMap | None = None


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _text(value: Any) -> str | None:
    return None if _missing(value) else str(value)


def parse_baseline(columns: Mapping[str, Any] | None, *, query_version: str) -> ParsedBaseline:
    if columns is None:
        return ParsedBaseline(BaselineStatus.NOT_QUERIED)
    observed_at = columns.get(BASELINE_OBSERVED_AT_COLUMN)
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        return ParsedBaseline(BaselineStatus.INVALID)
    context = columns.get("baseline_llm_context")
    if _missing(context):
        return ParsedBaseline(BaselineStatus.ABSENT, observed_at)
    try:
        data = json.loads(context) if isinstance(context, str) else dict(context)
        profile = BaselineProfile(
            query_version=query_version,
            case_received_date=data.get("case_received_date"),
            accounts=data["accounts"],
        )
        ref_map = BaselineRefMap(
            query_version=query_version,
            entries=[
                {"account_ref": e["account_ref"], "rel_id": _text(e.get("rel_id")), "cm15": _text(e.get("cm15"))}
                for e in columns.get("baseline_ref_map") or ()
            ],
        )
    except (ValueError, TypeError, KeyError, AttributeError):  # pydantic's ValidationError is a ValueError
        return ParsedBaseline(BaselineStatus.INVALID, observed_at)
    if {a.account_ref for a in profile.accounts} != {e.account_ref for e in ref_map.entries}:
        return ParsedBaseline(BaselineStatus.INVALID, observed_at)
    return ParsedBaseline(BaselineStatus.PRESENT, observed_at, profile, ref_map)
