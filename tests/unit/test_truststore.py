"""CFR_TRUSTSTORE=1 injects truststore early enough for boto's TLS setup (fresh interpreter each)."""

from __future__ import annotations

import os
import subprocess
import sys

PROBE = """
import ssl
import cfr_v2.artifacts  # imports boto3, as every entry point does
from botocore.httpsession import create_urllib3_context
create_urllib3_context()
print(ssl.SSLContext.__module__)
"""


def _probe(env_value: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CFR_TRUSTSTORE"}
    if env_value is not None:
        env["CFR_TRUSTSTORE"] = env_value
    return subprocess.run(
        [sys.executable, "-c", PROBE], check=False, env=env, capture_output=True, text=True, timeout=60
    )


def test_truststore_injected_before_boto():
    result = _probe("1")
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().startswith("truststore")


def test_truststore_off_by_default():
    result = _probe(None)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "ssl"
