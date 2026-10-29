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
