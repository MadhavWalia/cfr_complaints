"""The ``c04.routing`` stage handler (LDR-0021).

Reads the claim set from the upstream ``c02.pass2`` artifact (INTERNAL) and nothing else: no
request, transcript, baseline or case categories. Routes every claim (LDR-0020) and returns a
``RoutingResult`` bound to the claim set's hash; a later claim-set revision makes it stale.

What a decision depends on outside the code comes from the run's pinned release manifest: the
knowledge document (an artifact) and the procedure snapshot. The LightRAG store is the local
folder named after that snapshot (``cfr_v2.procedures.stores``), and must have been built from it.

Any failed claim fails the stage, so the runner retries it: a case never moves on with a partial
routing. A search the gateway blocks is not a failure: that claim is routed without sections.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dspy

from cfr_v2.artifacts import ArtifactStore
from cfr_v2.contracts import (
    AuthorizationContext,
    ClaimRoutingOutcome,
    ClaimSetRevision,
    Classification,
    ReleaseManifest,
    RoutingResult,
    canonical_json,
)
from cfr_v2.llm import ModelLoop, model_loop
from cfr_v2.runtime.stages import STAGES, StageHandler, StageInput, StageOutput
from cfr_v2.telemetry import get_logger, record_route, set_attributes, span

from .route import RoutedClaim, decision, route_claim
from .router import ROUTE_PROGRAM_ID, ROUTE_VERSION, ClaimInput, Router
from .verticals import VerticalCatalog

logger = get_logger(__name__)

STAGE_ID = "c04.routing"
ROUTE_CONCURRENCY = 4  # claims of one case routed at once; the rate gate still limits the process
# Each stage appends its output to the upstream refs, so the claim set sits at Pass 2's position.
_CLAIM_SET = next(i for i, s in enumerate(STAGES) if s.logical_task_id == "c02.pass2")
_READER = AuthorizationContext(principal=STAGE_ID, purpose="claim set, verticals, procedures")


class RoutingSetupError(RuntimeError):
    """The run's release can't route: a missing pin, store or document. Names refs only."""


@dataclass(frozen=True)
class RoutingContext:
    catalog: VerticalCatalog
    search: Any  # ProcedureSearch
    procedures_ref: str


RoutingResources = Callable[[str | None], RoutingContext]  # manifest ref -> what routing needs


def load_claim_set(store: ArtifactStore, ref: str) -> ClaimSetRevision:
    return ClaimSetRevision.model_validate_json(
        store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
    )


async def route_set(
    claim_set: ClaimSetRevision, context: RoutingContext, lm: dspy.LM, *, trace_id: str | None = None
) -> list[RoutedClaim]:
    """Every claim, a few at once. The first failure cancels the rest and is raised."""
    router = Router(context.catalog)
    gate = asyncio.Semaphore(ROUTE_CONCURRENCY)

    async def one(claim: ClaimInput) -> RoutedClaim:
        async with gate:
            return await route_claim(claim_set.case_id, claim, context.search, router, lm, trace_id=trace_id)

    claims = ClaimInput.from_claim_set(claim_set)
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(c)) for c in claims]
    except* Exception as failed:  # noqa: BLE001 - re-raised as is
        raise failed.exceptions[0] from None  # the runner classifies the error itself (a ModelError, say)
    return [t.result() for t in tasks]


def make_routing_handler(
    store: ArtifactStore, lm: dspy.LM, resources: RoutingResources, *, loop: ModelLoop | None = None
) -> StageHandler:
    def handle(inp: StageInput) -> StageOutput:
        claim_set = load_claim_set(store, inp.upstream_refs[_CLAIM_SET])
        with span("c04.routing", program=ROUTE_PROGRAM_ID, program_version=ROUTE_VERSION) as routing_span:
            routed: list[RoutedClaim] = []
            context = None
            if claim_set.claims:  # no claims (no transcript, say): nothing to route, nothing to pin
                try:
                    context = resources(inp.release_manifest_ref)
                except RoutingSetupError as exc:  # the runner logs only the type; this says what to fix
                    logger.error("routing: run %s can't route: %s", inp.case_run_id, exc)
                    raise
                routed = (loop or model_loop()).run(route_set(claim_set, context, lm, trace_id=inp.trace_id))
            result = RoutingResult(
                claim_set_hash=claim_set.claim_set_hash,
                program_id=ROUTE_PROGRAM_ID,
                program_version=ROUTE_VERSION,
                claims=tuple(r.routing for r in routed),
                procedures_ref=context and context.procedures_ref,
                verticals_sha256=context and context.catalog.sha256,
            )
            outcomes = Counter(r.routing.outcome for r in routed)
            set_attributes(
                routing_span,
                claims=len(routed),
                routed=outcomes[ClaimRoutingOutcome.ROUTED],
                unmapped=outcomes[ClaimRoutingOutcome.UNMAPPED],
                no_eligible=outcomes[ClaimRoutingOutcome.NO_ELIGIBLE_VERTICAL],
                searches_blocked=sum(r.blocked for r in routed),
            )
        for r in routed:
            record_route(decision(r.routing))
        return StageOutput(canonical_json(result))

    return handle


class PinnedRouting:
    """What routing needs for a run's pinned release manifest: the vertical definitions, cached
    per document, and one LightRAG store per worker process, opened on first use on the model
    loop (its call workers are bound to that loop) and closed by ``close``.

    ``manifests`` resolves a manifest ref; ``open_search`` opens the search over a snapshot. Both
    are parameters so tests can do without a database or a real store.
    """

    def __init__(
        self,
        store: ArtifactStore,
        manifests: Callable[[str], ReleaseManifest],
        open_search: Callable[[str, AsyncExitStack], Any],
        *,
        loop: ModelLoop | None = None,
    ) -> None:
        self.store = store
        self.manifests = manifests
        self.open_search = open_search
        self.loop = loop
        self._catalogs: dict[str, VerticalCatalog] = {}
        self._search: tuple[str, Any] | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = threading.Lock()

    def __call__(self, manifest_ref: str | None) -> RoutingContext:
        if manifest_ref is None:
            raise RoutingSetupError("the case run has no release manifest, so nothing says how to route")
        manifest = self.manifests(manifest_ref)
        if manifest.routing_verticals is None or manifest.procedure_snapshot is None:
            raise RoutingSetupError(
                f"release {manifest.ref} pins no routing_verticals or procedure_snapshot: it can't route"
            )
        with self._lock:
            return RoutingContext(
                self._catalog(manifest.routing_verticals),
                self._open(manifest.procedure_snapshot),
                manifest.procedure_snapshot,
            )

    def _catalog(self, ref: str) -> VerticalCatalog:
        catalog = self._catalogs.get(ref)
        if catalog is None:
            raw = self.store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
            catalog = self._catalogs[ref] = VerticalCatalog.parse(raw.decode("utf-8"))
            logger.info("routing: %d verticals from %s", len(catalog.ids), ref)
        return catalog

    def _open(self, procedures_ref: str) -> Any:
        if self._search is not None:
            opened, search = self._search
            if opened != procedures_ref:
                raise RoutingSetupError(
                    f"this worker's LightRAG store serves {opened}; release asks for {procedures_ref}"
                )
            return search
        stack = AsyncExitStack()

        async def enter() -> Any:
            try:
                return await self.open_search(procedures_ref, stack)
            except BaseException:
                await stack.aclose()
                raise

        search = (self.loop or model_loop()).run(enter())
        self._search, self._stack = (procedures_ref, search), stack
        logger.info("routing: LightRAG store opened for %s", procedures_ref)
        return search

    def close(self) -> None:
        with self._lock:
            stack, self._stack, self._search = self._stack, None, None
        if stack is not None:
            (self.loop or model_loop()).run(stack.aclose())


def lightrag_opener(store: ArtifactStore, workdir: Path | None, embedder: Any, client: Any):
    """Opens LightRAG for a snapshot, after checking the store was built from it. The store is
    ``workdir`` when given, else the snapshot's own folder (or ``CFR_LIGHTRAG_WORKDIR``)."""

    async def open_search(procedures_ref: str, stack: AsyncExitStack) -> Any:
        from cfr_v2.procedures import corpus_passages
        from cfr_v2.procedures.lightrag_engine import (
            GatewayLlm,
            LightRagProcedureSearch,
            StoreMismatchError,
            check_store,
            open_rag,
        )
        from cfr_v2.procedures.stores import override_hint, store_dir

        folder = workdir if workdir is not None else store_dir(procedures_ref)
        try:
            check_store(folder, procedures_ref)
        except StoreMismatchError as exc:
            hint = "" if workdir is not None else override_hint(procedures_ref)
            raise RoutingSetupError(f"{exc}{hint}") from exc
        passages = corpus_passages(store, procedures_ref)
        llm = GatewayLlm(client)
        rag = await stack.enter_async_context(open_rag(folder, embedder, llm))
        return LightRagProcedureSearch(rag, procedures_ref, passages, llm)

    return open_search
