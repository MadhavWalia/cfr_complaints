"""Test stand-in for the private ``ee_logging`` package (see ../README.md)."""
import logging


def configure_telemetry(elf_config, redaction_config=None):
    raise NotImplementedError("ee_logging stub: telemetry is unconfigured")


def get_logger(name, *, level=None):
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger


def aobserve(func):
    return func
