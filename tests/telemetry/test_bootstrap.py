from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from cfr_v2.telemetry import _state, bootstrap, init_telemetry, shutdown_telemetry


def test_init_enables_on_success(monkeypatch, fake_elf_config):
    telemetry_obj = MagicMock()
    monkeypatch.setattr("ee_config.load_config", MagicMock(return_value=fake_elf_config))
    monkeypatch.setattr(
        "ee_logging.configure_telemetry", MagicMock(return_value=telemetry_obj)
    )
    monkeypatch.setattr(bootstrap, "_instrument_httpx", MagicMock())
    monkeypatch.setattr(bootstrap, "_rewire_registered_file_loggers", MagicMock())
    monkeypatch.setattr("atexit.register", MagicMock())

    result = init_telemetry()

    assert result is telemetry_obj
    assert _state.enabled is True
    assert _state.telemetry is telemetry_obj


def test_init_idempotent(monkeypatch):
    sentinel = object()
    _state.initialised = True
    _state.telemetry = sentinel
    load_config = MagicMock()
    monkeypatch.setattr("ee_config.load_config", load_config)

    assert init_telemetry() is sentinel
    load_config.assert_not_called()


def test_init_disabled_on_config_failure(monkeypatch):
    monkeypatch.setattr(
        "ee_config.load_config", MagicMock(side_effect=RuntimeError("bad"))
    )
    assert init_telemetry() is None
    assert _state.enabled is False


def test_init_disabled_when_elf_section_missing(monkeypatch):
    cfg = MagicMock()
    cfg.elf = None
    monkeypatch.setattr("ee_config.load_config", MagicMock(return_value=cfg))

    assert init_telemetry() is None
    assert _state.enabled is False


def test_shutdown_noop_when_disabled():
    _state.enabled = False
    _state.telemetry = None
    shutdown_telemetry()  # must not raise


def test_shutdown_flushes_providers(monkeypatch):
    tracer_provider = MagicMock()
    meter_provider = MagicMock()
    _state.enabled = True
    # logger_provider=None exercises the "provider has no shutdown" skip branch.
    _state.telemetry = SimpleNamespace(
        tracer_provider=tracer_provider,
        logger_provider=None,
        meter_provider=meter_provider,
    )
    monkeypatch.setattr(bootstrap, "_uninstrument_httpx", MagicMock())
    monkeypatch.setattr(bootstrap, "_close_httpx2_clients", MagicMock())
    monkeypatch.setattr(bootstrap, "_detach_otel_log_handlers", MagicMock())

    shutdown_telemetry()

    tracer_provider.shutdown.assert_called_once()
    meter_provider.shutdown.assert_called_once()
    assert _state.enabled is False


def test_dev_otlp_used_when_ee_logging_absent(monkeypatch):
    providers = SimpleNamespace(tracer_provider=MagicMock(), meter_provider=MagicMock(), logger_provider=MagicMock())
    configure = MagicMock(return_value=providers)
    monkeypatch.setattr(bootstrap.dev, "configure_otlp", configure)
    monkeypatch.setattr("atexit.register", MagicMock())
    monkeypatch.setenv("CFR_OTLP_ENDPOINT", "http://localhost:4318")

    assert init_telemetry(service_name="cfr-v2-worker") is providers
    configure.assert_called_once_with("http://localhost:4318", "cfr-v2-worker")
    assert _state.dev_exporter is True and _state.enabled is False

    monkeypatch.setattr(bootstrap, "_detach_otel_log_handlers", MagicMock())
    shutdown_telemetry()
    providers.tracer_provider.shutdown.assert_called_once()
    assert _state.dev_exporter is False


def test_no_dev_otlp_without_endpoint(monkeypatch):
    configure = MagicMock()
    monkeypatch.setattr(bootstrap.dev, "configure_otlp", configure)
    monkeypatch.delenv("CFR_OTLP_ENDPOINT", raising=False)
    assert init_telemetry() is None
    configure.assert_not_called()


def test_dev_otlp_skipped_when_exporter_missing(monkeypatch):
    monkeypatch.setattr(bootstrap.dev, "configure_otlp", MagicMock(return_value=None))
    monkeypatch.setenv("CFR_OTLP_ENDPOINT", "http://localhost:4318")
    assert init_telemetry() is None
    assert _state.dev_exporter is False


def test_configure_otlp_builds_providers_for_each_signal():
    from cfr_v2.telemetry import dev

    providers = dev.configure_otlp("http://localhost:4318/", "svc", install=False)
    try:
        exporter = providers.tracer_provider._active_span_processor._span_processors[0].span_exporter
        assert exporter._endpoint == "http://localhost:4318/v1/traces"
        assert providers.tracer_provider.resource.attributes["service.name"] == "svc"
    finally:
        for name in ("tracer_provider", "meter_provider", "logger_provider"):
            getattr(providers, name).shutdown()


def test_configure_otlp_returns_none_without_the_exporter(monkeypatch):
    import builtins

    from cfr_v2.telemetry import dev

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("opentelemetry.exporter.otlp"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert dev.configure_otlp("http://localhost:4318", "svc", install=False) is None
