"""Dev-only OTLP export when ee-logging is not installed (LDR-0012, OQ-25).

Sends traces, metrics and logs to a local OpenTelemetry Collector (the
``observability`` compose profile: Tempo, Prometheus, Loki, Grafana). Enterprise
hosts never reach this: ``ee_logging`` configures its own exporters there.
Opt in with ``CFR_OTLP_ENDPOINT`` (e.g. ``http://localhost:4318``).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

METRIC_EXPORT_INTERVAL_MS = 15_000


def configure_otlp(endpoint: str, service_name: str, *, install: bool = True) -> Any | None:
    """Build OTLP/HTTP providers for ``endpoint``; None if the exporter isn't installed.

    ``install`` sets them as the OTel globals and attaches a log handler to the
    root logger (tests pass False, since the globals can be set only once).
    """
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        logging.getLogger("cfr_v2.telemetry").warning(
            "CFR_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp-proto-http is not installed"
        )
        return None
    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    base = endpoint.rstrip("/")
    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{base}/v1/metrics"), export_interval_millis=METRIC_EXPORT_INTERVAL_MS
            )
        ],
    )
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs")))

    if install:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        set_logger_provider(logger_provider)
        logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    return SimpleNamespace(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        instruments=None,
    )
