"""Offline stand-ins for C01: a regex masker and synthetic ``current_cases.sql`` rows.

All values are invented (``example.com`` addresses, 555 phone numbers, made-up
names). Nothing here is derived from real case data.
"""
from __future__ import annotations

import random
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from .redaction import PII_TOKEN
from .source import SOURCE_COLUMNS

_EMAIL = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
_PHONE = r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b"
_CARD = r"\b\d{4}[ -]?\d{6}[ -]?\d{5}\b|\b\d{15,16}\b"


class RegexMasker:
    """Mimics ``ee_utils`` masking mode: emails, phone and card numbers, and listed names."""

    version = "regex-fake-1"

    def __init__(self, names: Iterable[str] = ()) -> None:
        alternatives = [_EMAIL, _PHONE, _CARD] + [rf"\b{re.escape(n)}\b" for n in sorted(names, key=len, reverse=True)]
        self._pattern = re.compile("|".join(f"(?:{a})" for a in alternatives))

    def mask(self, text: str) -> str:
        return self._pattern.sub(PII_TOKEN, text)


FIRST_NAMES = ("Avery", "Jordan", "Riley", "Morgan", "Casey", "Quinn", "Rowan", "Sage")
LAST_NAMES = ("Lindqvist", "Okafor", "Marchetti", "Tanaka", "Delacroix", "Brennan")
AGENT_NAMES = ("Dana", "Elliot", "Harper")
CATEGORIES = (
    ("Billing", "Fee Dispute"),
    ("Billing", "Interest Charge"),
    ("Servicing", "Card Replacement"),
    ("Rewards", "Points Not Credited"),
    ("Collections", "Payment Arrangement"),
)
RESOLUTIONS = ("Resolved - Credit Issued", "Resolved - Explanation Provided", "Escalated", "No Action Required")

_TURNS = (
    "Agent: Thank you for calling, my name is {agent}. How can I help you today?",
    "Customer: Hi, this is {first} {last}. I was charged a fee I don't recognise on my statement.",
    "Agent: I'm sorry to hear that. Can you confirm the email on file?",
    "Customer: Sure, it's {email}.",
    "Agent: Thanks. And a good callback number?",
    "Customer: {phone}. Honestly this is the third time I've called about this.",
    "Agent: I understand your frustration. I can see the {subcategory} on the account.",
    "Customer: I was told last month it would be reversed and it never was.",
    "Agent: Let me review that for you and see what I can do.",
)


def synthetic_names() -> tuple[str, ...]:
    """Every name used in synthetic transcripts, for ``RegexMasker(names=...)``."""
    return FIRST_NAMES + LAST_NAMES + AGENT_NAMES


def synthetic_transcript(rng: random.Random, subcategory: str, *, calls: int = 2) -> str:
    """Two or more calls joined with ``|`` as V1's ``msg_tx2`` aggregation does."""
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    values = {
        "agent": rng.choice(AGENT_NAMES),
        "first": first,
        "last": last,
        "email": f"{first.lower()}.{last.lower()}@example.com",
        "phone": f"555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
        "subcategory": subcategory.lower(),
    }
    return "|".join(" ".join(t.format(**values) for t in _TURNS[: rng.randint(5, len(_TURNS))]) for _ in range(calls))


def synthetic_row(i: int, *, seed: int = 0, **overrides: Any) -> dict[str, Any]:
    """One row with every ``current_cases.sql`` output column."""
    rng = random.Random(seed * 100_003 + i)
    category, subcategory = rng.choice(CATEGORIES)
    resolved = datetime(2026, 9, 1, 12, tzinfo=timezone.utc) + timedelta(hours=7 * i)
    row: dict[str, Any] = {
        "case_no": f"SYN{seed:02d}{i:06d}",
        "latest_rslv_optn": rng.choice(RESOLUTIONS),
        "discrimination_type": None,
        "orig_rslv_ts": resolved,
        "reg_sta": rng.choice(("Y", "N")),
        "prim_ctgy": category,
        "prim_subctgy": subcategory,
        "cqat_flag": rng.choice(("Yes", "No")),
        "pie_flag": "No",
        "creat_opr_id": f"OPR{rng.randint(10000, 99999)}",
        "cm11": f"{rng.randint(10**10, 10**11 - 1)}",
        "cm15": f"{rng.randint(10**14, 10**15 - 1)}",
        "case_note": "Customer disputes a fee; reviewed account history.",
        "case_ds": f"{category} / {subcategory}",
        "ccp_note": None,
        "pend_resp_note": None,
        "req_note": None,
        "pend_req_note": None,
        "latest_rslv_note": "Explained the charge to the customer.",
        "rcpt_ts": resolved - timedelta(days=2),
        "emp_ntwk_id": f"agent{rng.randint(100, 999)}",
        "potl_defect_flag": rng.choice(("Yes", None)),
        "err_type": rng.choice(("Process", "System", None)),
        "err_sub_type": None,
        "case_type": "Complaint",
        "intrct_ids": [f"IX{i:06d}{k}" for k in range(2)],
        "doc_id": [f"DOC{i:06d}"],
        "exact_match_flag": rng.choice(("Exact-Matched", "Not-Matched")),
        "msg_tx2": synthetic_transcript(rng, subcategory),
    }
    assert tuple(row) == SOURCE_COLUMNS
    row.update(overrides)
    return row


def synthetic_rows(n: int, *, seed: int = 0) -> list[dict[str, Any]]:
    return [synthetic_row(i, seed=seed) for i in range(n)]
