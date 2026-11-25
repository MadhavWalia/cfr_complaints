"""C04's search keywords, one call per case (LDR-0044)."""

from __future__ import annotations

import json
import re

import pytest

from cfr_v2.contracts import ProcedureSearchResult
from cfr_v2.llm import FakeModelClient, ModelBlockedError
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.routing import route
from cfr_v2.routing.keywords import KEYWORDS_PROGRAM_ID, KEYWORDS_VERSION, MOST, claim_keywords, make_signature
from cfr_v2.routing.router import ClaimInput

CLAIMS = [
    ClaimInput("c1", "A late fee was charged after the plan was agreed"),
    ClaimInput("c2", "The card was declined at the till"),
    ClaimInput("c3", "The account was closed without notice"),
]


def _ids(request) -> list[str]:
    return re.findall(r'"id": "([^"]+)"', "".join(t.content for t in request.messages))


def _reply(request, *, skip=(), extra=None):
    items = [
        {"claim_id": i, "low_level": [f"thing {i}", f"thing {i}", " other  thing "], "high_level": [f"theme {i}"]}
        for i in _ids(request)
        if i not in skip
    ]
    return {"keywords": items + (extra or [])}


async def test_one_call_writes_every_claims_keywords_from_its_statement_only():
    client = FakeModelClient(responder=_reply)
    found = await claim_keywords("GC-1", CLAIMS, gateway_lm(client, "m"), trace_id="t1")
    [request] = client.requests
    assert (request.program_id, request.program_version, request.case_id) == (
        KEYWORDS_PROGRAM_ID,
        KEYWORDS_VERSION,
        "GC-1",
    )
    assert request.trace_id == "t1" and request.redacted
    sent = json.loads(re.search(r"\[\{.*\}\]", "".join(t.content for t in request.messages)).group(0))
    assert sent == [{"id": c.claim_id, "statement": c.statement} for c in CLAIMS]
    assert found.by_claim["c2"].low_level == ("thing c2", "other thing")  # repeats and spacing cleaned
    assert found.by_claim["c2"].high_level == ("theme c2",) and found.calls == 1 and not found.blocked


def test_the_claim_ids_are_an_enum_in_the_schema():
    schema = json.dumps(
        make_signature(["c1", "c2"]).output_fields["keywords"].annotation.__args__[0].model_json_schema()
    )
    assert '"enum": ["c1", "c2"]' in schema


async def test_a_claim_left_out_is_searched_by_its_statement():
    client = FakeModelClient(responder=lambda r: _reply(r, skip={"c3"}))
    found = await claim_keywords("GC-1", CLAIMS, gateway_lm(client, "m"))
    assert "c3" not in found.by_claim and not found.blocked
    assert found.request_keywords(CLAIMS[2]) == {
        "high_level_keywords": (),
        "low_level_keywords": (CLAIMS[2].statement,),
    }
    assert found.request_keywords(CLAIMS[0])["low_level_keywords"][0] == "thing c1"


async def test_a_blocked_call_is_split_until_only_the_blocked_claim_goes_without():
    def blocks_c2(request):
        if "c2" in _ids(request):
            raise ModelBlockedError("blocked")
        return _reply(request)

    client = FakeModelClient(responder=blocks_c2)
    found = await claim_keywords("GC-1", CLAIMS, gateway_lm(client, "m"))
    assert found.blocked == {"c2"} and set(found.by_claim) == {"c1", "c3"}
    assert [_ids(r) for r in client.requests] == [["c1", "c2", "c3"], ["c1"], ["c2", "c3"], ["c2"], ["c3"]]


async def test_keywords_are_capped_per_list():
    many = {"claim_id": "c1", "low_level": [f"k{i}" for i in range(20)], "high_level": []}
    client = FakeModelClient(responder=lambda r: {"keywords": [many]})
    found = await claim_keywords("GC-1", CLAIMS[:1], gateway_lm(client, "m"))
    assert len(found.by_claim["c1"].low_level) == MOST


class Search:
    def __init__(self):
        self.requests = []

    async def search_many(self, requests):
        self.requests += requests
        return [ProcedureSearchResult(procedures_ref="sha256:" + "0" * 64, hits=()) for _ in requests]


async def test_a_cases_searches_take_the_keywords_and_a_blocked_claim_is_reported_blocked():
    def blocks_c2(request):
        if "c2" in _ids(request):
            raise ModelBlockedError("blocked")
        return _reply(request)

    search = Search()
    found = await route.search_claims("GC-1", CLAIMS, search, gateway_lm(FakeModelClient(responder=blocks_c2), "m"))
    assert [r.query for r in search.requests] == [c.statement for c in CLAIMS]
    assert search.requests[0].low_level_keywords == ("thing c1", "other thing")
    assert search.requests[1].low_level_keywords == (CLAIMS[1].statement,)  # no keywords: its statement
    assert [f.blocked for f in found] == [False, True, False]


async def test_a_rate_limit_is_not_swallowed():
    from cfr_v2.llm import ModelRateLimitedError

    def limited(request):
        raise ModelRateLimitedError("busy")

    with pytest.raises(ModelRateLimitedError):
        await claim_keywords("GC-1", CLAIMS, gateway_lm(FakeModelClient(responder=limited), "m"))
