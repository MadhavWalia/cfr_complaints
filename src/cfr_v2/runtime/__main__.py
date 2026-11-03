"""Worker process: run case-run jobs from the Postgres queue until stopped.

    python -m cfr_v2.runtime                      # serve every lane
    python -m cfr_v2.runtime --lanes realtime     # one lane only

Stops on SIGTERM or Ctrl+C: waits up to ``--drain-timeout`` for running jobs, then
hands the rest back so another worker takes them at once (LDR-0011). Configuration
comes from the environment (see ``.env.example``).
"""

from __future__ import annotations

import argparse
import signal
import threading

from cfr_v2.artifacts import S3ArtifactStore, S3Settings
from cfr_v2.contracts import Lane
from cfr_v2.telemetry import get_logger, init_telemetry, shutdown_telemetry

from .app import start_runtime, stop_runtime
from .settings import RuntimeSettings
from .stages import StageHandler, default_handlers

logger = get_logger(__name__)


def stage_handlers(store: S3ArtifactStore) -> dict[str, StageHandler]:
    """Real components where they exist (C02 Pass 1 and Pass 2), placeholders for the rest.

    Pass 2's evidence source taxonomy is ``CFR_EVIDENCE_SOURCES_VERSION`` (default
    ``evidence-sources-1``) from ``registry/``; each claim set records the version used.
    """
    import os

    from cfr_v2.llm import GatewayModelClient, GatewaySettings
    from cfr_v2.llm.dspy_lm import gateway_lm
    from cfr_v2.registry.evidence_sources import DEFAULT_VERSION, load_taxonomy
    from cfr_v2.understanding import make_pass1_handler, make_pass2_handler

    settings = GatewaySettings.from_env()
    lm = gateway_lm(GatewayModelClient(settings), settings.model_id)
    taxonomy = load_taxonomy(os.environ.get("CFR_EVIDENCE_SOURCES_VERSION", DEFAULT_VERSION))
    return {
        **default_handlers(),
        "c02.pass1": make_pass1_handler(store, lm),
        "c02.pass2": make_pass2_handler(store, lm, taxonomy),
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
    start_runtime(RuntimeSettings.from_env(), store, handlers=stage_handlers(store), listen_lanes=lanes)
    try:
        stop.wait()
        logger.info("worker: stopping (drain timeout %.0fs)", args.drain_timeout)
    finally:
        stop_runtime(workflow_completion_timeout_sec=args.drain_timeout)
        shutdown_telemetry()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
