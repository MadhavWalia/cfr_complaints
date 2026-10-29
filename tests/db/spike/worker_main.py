"""Worker process for the DBOS spike tests (crash/resume, multi-process throughput).

Usage (env-driven, see test_crash_resume.py and test_throughput.py):
    CFR_SPIKE_MODE=submit|serve  [CFR_SPIKE_CASE=<id>]
    [CFR_SPIKE_BLOCK_TASK=<task> CFR_SPIKE_MARKER=<path>]
    [CFR_SPIKE_EXECUTOR_ID=<id>] [CFR_SPIKE_BATCH_CONCURRENCY=<n>] [CFR_SPIKE_READY_MARKER=<path>]

``submit`` admits one case and enqueues it; ``serve`` only runs the runtime,
which recovers this executor's pending workflows on launch. If a block task is
set, its handler touches the marker file and hangs until the process is killed.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cfr_v2.artifacts import S3ArtifactStore, S3Credentials, S3Settings
from cfr_v2.contracts import AdmissionKey, IngestionRequest
from cfr_v2.runtime import RuntimeSettings, StageInput, StageOutput, default_handlers, start_runtime, submit
from cfr_v2.runtime.stages import placeholder_handler


def main() -> int:
    env = os.environ
    settings = RuntimeSettings(
        state_database_url=env["CFR_STATE_DATABASE_URL"],
        dbos_system_database_url=env["CFR_DBOS_SYSTEM_DATABASE_URL"],
        queue_polling_interval_sec=0.1,
        executor_id=env.get("CFR_SPIKE_EXECUTOR_ID", "local"),
        batch_queue_concurrency=int(env.get("CFR_SPIKE_BATCH_CONCURRENCY", "50")),
    )
    s3 = json.loads(env["CFR_SPIKE_S3"])
    store = S3ArtifactStore(
        S3Settings(
            endpoint_url=s3["endpoint_url"],
            region_name="us-east-1",
            credentials=S3Credentials(s3["access_key"], s3["secret_key"]),
            restricted_credentials=None,
            ca_bundle=None,
            bucket=s3["bucket"],
            restricted_bucket=s3["restricted_bucket"],
        )
    )

    handlers = dict(default_handlers())
    block_task = env.get("CFR_SPIKE_BLOCK_TASK")
    if block_task:
        marker = Path(env["CFR_SPIKE_MARKER"])

        def blocking(inp: StageInput) -> StageOutput:
            marker.write_text(inp.case_run_id)
            while True:
                time.sleep(3600)

        handlers[block_task] = blocking
    else:
        handlers = {k: placeholder_handler for k in handlers}

    start_runtime(settings, store, handlers=handlers)
    if env.get("CFR_SPIKE_READY_MARKER"):
        Path(env["CFR_SPIKE_READY_MARKER"]).write_text(settings.executor_id)
    if env["CFR_SPIKE_MODE"] == "submit":
        request = IngestionRequest(
            key=AdmissionKey(source_system="icare", source_case_id=env["CFR_SPIKE_CASE"], ingestion_revision=0),
            raw_transcript_ref=None,
            received_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
        )
        submit(request, release_manifest_ref="spike@0")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
