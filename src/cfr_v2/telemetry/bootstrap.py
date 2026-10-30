from __future__ import annotations

import atexit
import logging
import os
from typing import Any, Optional

from . import _state, dev
from .api import get_logger
from .http import _close_httpx2_clients, _instrument_httpx, _uninstrument_httpx
from .logging import _detach_otel_log_handlers, _rewire_registered_file_loggers


def init_telemetry(config_path: Optional[str] = None, *, service_name: str = "cfr-v2") -> Any:
    """Start telemetry once per process; never raises.

    With ee-logging: exporters from the ``elf:`` section of the ee-config file.
    Without it: stdlib logging, plus dev-only OTLP export when ``CFR_OTLP_ENDPOINT``
    is set (``service_name`` is used only there; ee-logging takes it from config).
    """
    if _state.initialised:
        return _state.telemetry

    _state.initialised = True
    try:
        from ee_config import load_config
        from ee_logging import configure_telemetry

        cfg = load_config(config_path)
        if getattr(cfg, "elf", None) is None:
            raise RuntimeError("no 'elf' section found in configuration")

        _state.telemetry = configure_telemetry(cfg.elf, cfg.redaction)
        _state.enabled = True
        _rewire_registered_file_loggers()
        _instrument_httpx()

        atexit.register(shutdown_telemetry)
        get_logger("cfr_v2.telemetry").info(
            "Telemetry initialised (service=%s)", cfg.elf.service_name
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the pipeline
        _state.enabled = False
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("cfr_v2.telemetry").warning(
            "Telemetry disabled, falling back to stdlib logging: %s", exc
        )
        endpoint = os.environ.get("CFR_OTLP_ENDPOINT")
        if endpoint:
            providers = dev.configure_otlp(endpoint, service_name)
            if providers is not None:
                _state.telemetry = providers
                _state.dev_exporter = True
                atexit.register(shutdown_telemetry)
                logging.getLogger("cfr_v2.telemetry").info(
                    "Dev OTLP export to %s (service=%s)", endpoint, service_name
                )
    return _state.telemetry


def shutdown_telemetry() -> None:
    if _state.telemetry is None or not (_state.enabled or _state.dev_exporter):
        return

    if _state.enabled:
        _uninstrument_httpx()
        _close_httpx2_clients()
    _detach_otel_log_handlers()

    for provider_name in ("tracer_provider", "logger_provider", "meter_provider"):
        provider = getattr(_state.telemetry, provider_name, None)
        shutdown = getattr(provider, "shutdown", None)
        if shutdown is None:
            continue
        try:
            shutdown()
        except Exception:  # noqa: BLE001 - shutdown is best-effort  # pragma: no cover
            pass
    _state.enabled = False
    _state.dev_exporter = False
