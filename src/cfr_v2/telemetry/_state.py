from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

# Shared mutable telemetry state. Kept in one module so every telemetry
# submodule observes the same values after init_telemetry() runs.
telemetry: Any = None
enabled: bool = False
initialised: bool = False
httpx_instrumented: bool = False
# Dev-only OTLP providers in use (ee-logging absent, CFR_OTLP_ENDPOINT set).
dev_exporter: bool = False

# File logger registrations made before init_telemetry(). They are rewired to
# the OTel logger provider once telemetry is initialised.
file_logger_configs: dict[str, tuple[Path, int | str | None]] = {}

httpx2_async_client: Any = None
httpx2_sync_client: Any = None
httpx2_clients_lock = threading.Lock()
