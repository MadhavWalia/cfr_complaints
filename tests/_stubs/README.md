# Test stubs for private enterprise packages

`ee_logging` and `ee_config` come from the internal Artifactory. These minimal
stand-ins exist only so tests can `monkeypatch` their attributes without the real
packages installed. `tests/conftest.py` appends this directory to `sys.path`
*after* site-packages, so the real packages win when present. Setup entry points
(`configure_telemetry`, `load_config`) raise, so code under test behaves as if
the package were unconfigured unless a test patches them; helpers such as
`get_logger` fall back to the stdlib.
