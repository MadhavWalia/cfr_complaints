"""Worker process: run case-run jobs from the Postgres queue until stopped.

    python -m cfr_v2.runtime                      # serve every lane
    python -m cfr_v2.runtime --lanes realtime     # one lane only

Stops on SIGTERM or Ctrl+C: waits up to ``--drain-timeout`` for running jobs, then
hands the rest back so another worker takes them at once (LDR-0011). Configuration
comes from the environment (see ``.env.example``).
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
from collections.abc import Callable
from pathlib import Path

from cfr_v2.artifacts import S3ArtifactStore, S3Settings
from cfr_v2.contracts import AuthorizationContext, Lane
from cfr_v2.telemetry import get_logger, init_telemetry, shutdown_telemetry

from .app import start_runtime, stop_runtime
from .settings import RuntimeSettings
from .stages import StageHandler, default_handlers

logger = get_logger(__name__)
_ROUTING_READER = AuthorizationContext(principal="c04.routing", purpose="release manifest")


def stage_handlers(store: S3ArtifactStore, on_stop: list[Callable[[], None]] | None = None) -> dict[str, StageHandler]:
    """Real components where they exist (C02 Pass 1 and Pass 2, C03, C04), placeholders for the rest.

    Pass 2's evidence source taxonomy is the one the run's pinned release manifest names
    (``evidence-sources-1`` when it names none); each claim set records the version used.
    C04 routes with the knowledge document and procedure snapshot the manifest pins, over the
    LightRAG store in ``CFR_LIGHTRAG_WORKDIR``, opened on first use (LDR-0021). ``on_stop`` gets
    what must be closed when the worker stops.
    """
    from cfr_v2.llm import GatewayEmbeddingClient, GatewayModelClient, GatewaySettings
    from cfr_v2.llm.dspy_lm import gateway_lm
    from cfr_v2.registry import load_pinned
    from cfr_v2.registry.evidence_sources import PinnedTaxonomies
    from cfr_v2.routing.handler import PinnedRouting, lightrag_opener, make_routing_handler
    from cfr_v2.selection import make_selection_handler
    from cfr_v2.understanding import make_pass1_handler, make_pass2_handler

    from . import context

    settings = GatewaySettings.from_env()
    client = GatewayModelClient(settings)
    lm = gateway_lm(client, settings.model_id)
    taxonomies = PinnedTaxonomies(store, db=lambda: context.current().db)  # the pool opens with the runtime
    workdir = os.environ.get("CFR_LIGHTRAG_WORKDIR")
    routing = PinnedRouting(
        store,
        lambda ref: load_pinned(ref, store=store, db=context.current().db, authorization=_ROUTING_READER),
        lightrag_opener(store, Path(workdir) if workdir else None, GatewayEmbeddingClient(), client),
    )
    if on_stop is not None:
        on_stop.append(routing.close)
    return {
        **default_handlers(),
        "c02.pass1": make_pass1_handler(store, lm),
        "c02.pass2": make_pass2_handler(store, lm, taxonomies),
        "c03.primary_selection": make_selection_handler(store, lm),
        "c04.routing": make_routing_handler(store, lm, routing),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.runtime", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lanes", default="batch,realtime", help="comma-separated lanes to serve (default: batch,realtime)"
    )
    parser.add_argument("--drain-timeout", type=float, default=30.0, help="seconds to wait for running jobs on stop")
    args = parser.parse_args(argv)
    try:
        lanes = [Lane(name.strip()) for name in args.lanes.split(",") if name.strip()]
    except ValueError as exc:
        parser.error(str(exc))
    if not lanes:
        parser.error("at least one lane is required")

    init_telemetry(service_name="cfr-v2-worker")  # ee_config resolves its file, as for redaction
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    store = S3ArtifactStore(S3Settings.from_env())
    on_stop: list[Callable[[], None]] = []
    start_runtime(RuntimeSettings.from_env(), store, handlers=stage_handlers(store, on_stop), listen_lanes=lanes)
    try:
        stop.wait()
        logger.info("worker: stopping (drain timeout %.0fs)", args.drain_timeout)
    finally:
        stop_runtime(workflow_completion_timeout_sec=args.drain_timeout)
        for close in on_stop:  # after the runtime: no stage is using them any more
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - stopping goes on; the error type is logged
                logger.warning("worker: could not close %s: %s", close, type(exc).__name__)
        shutdown_telemetry()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
