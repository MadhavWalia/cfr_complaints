from __future__ import annotations

from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ConnectionClosedError
from moto import mock_aws

from cfr_v2.artifacts import (
    ArtifactAccessDeniedError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    S3ArtifactStore,
    S3Credentials,
    S3Settings,
    object_key,
)
from cfr_v2.artifacts.s3 import ReconnectingClient, client_config
from cfr_v2.contracts import AuthorizationContext, Classification, sha256_hex

INTERNAL_READER = AuthorizationContext(principal="worker", purpose="cfr")
RESTRICTED_READER = AuthorizationContext(
    principal="intake", purpose="cfr", allowed_classifications=frozenset({"INTERNAL", "RESTRICTED"})
)


def _settings(**overrides) -> S3Settings:
    base = dict(
        endpoint_url=None,
        region_name="us-east-1",
        credentials=S3Credentials("test", "test"),
        restricted_credentials=None,
        ca_bundle=None,
        bucket="cfr-v2-test-artifacts",
        restricted_bucket="cfr-v2-test-artifacts-restricted",
    )
    return S3Settings(**(base | overrides))


@pytest.fixture
def s3():
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


@pytest.fixture
def store(s3):
    settings = _settings()
    clients = {c: ReconnectingClient(lambda: boto3.client("s3", region_name="us-east-1")) for c in Classification}
    st = S3ArtifactStore(settings, clients=clients)
    st.ensure_buckets()
    return st


def test_put_is_content_addressed(store, s3):
    a = store.put(b"hello", media_type="text/plain", classification=Classification.INTERNAL)
    b = store.put(b"hello", media_type="text/plain", classification=Classification.INTERNAL)
    assert a == b
    assert a.metadata.ref == f"sha256:{sha256_hex(b'hello')}"
    assert a.key == object_key(sha256_hex(b"hello"))
    listed = s3.list_objects_v2(Bucket="cfr-v2-test-artifacts")["Contents"]
    assert [o["Key"] for o in listed] == [a.key]


def test_second_put_hits_conditional_write(store, s3):
    """moto honours IfNoneMatch, so the 412 path is exercised for real."""
    first = store.put(b"x", media_type="text/plain", classification=Classification.INTERNAL)
    with pytest.raises(s3.exceptions.ClientError) as exc_info:
        s3.put_object(Bucket=first.bucket, Key=first.key, Body=b"x", IfNoneMatch="*")
    assert exc_info.value.response["Error"]["Code"] == "PreconditionFailed"
    assert store.put(b"x", media_type="text/plain", classification=Classification.INTERNAL) == first


def test_round_trip_and_classification_buckets(store):
    stored = store.put(b"secret", media_type="text/plain", classification=Classification.RESTRICTED)
    assert stored.bucket == "cfr-v2-test-artifacts-restricted"
    assert store.get(stored.metadata.ref, classification=Classification.RESTRICTED, authorization=RESTRICTED_READER) == b"secret"


def test_restricted_read_denied_without_permission(store):
    stored = store.put(b"secret", media_type="text/plain", classification=Classification.RESTRICTED)
    with pytest.raises(ArtifactAccessDeniedError):
        store.get(stored.metadata.ref, classification=Classification.RESTRICTED, authorization=INTERNAL_READER)


def test_corrupted_object_fails_integrity(store, s3):
    stored = store.put(b"original", media_type="text/plain", classification=Classification.INTERNAL)
    s3.put_object(Bucket=stored.bucket, Key=stored.key, Body=b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.get(stored.metadata.ref, classification=Classification.INTERNAL, authorization=INTERNAL_READER)


def test_missing_object(store):
    with pytest.raises(ArtifactNotFoundError):
        store.get("sha256:" + "0" * 64, classification=Classification.INTERNAL, authorization=INTERNAL_READER)


def test_size_guard(store):
    small = S3ArtifactStore(store.settings, clients=store._clients, max_bytes=4)
    with pytest.raises(ArtifactTooLargeError):
        small.put(b"12345", media_type="text/plain", classification=Classification.INTERNAL)


async def test_async_wrappers(store):
    stored = await store.aput(b"async", media_type="text/plain", classification=Classification.INTERNAL)
    data = await store.aget(stored.metadata.ref, classification=Classification.INTERNAL, authorization=INTERNAL_READER)
    assert data == b"async"


def test_warns_when_restricted_credentials_missing(caplog):
    S3ArtifactStore(_settings(), clients={})
    assert "separation is logical only" in caplog.text


def test_reconnects_on_dropped_connection():
    broken = MagicMock()
    broken.get_object.side_effect = ConnectionClosedError(endpoint_url="http://s3")
    healthy = MagicMock()
    healthy.get_object.return_value = {"ok": True}
    factory = MagicMock(side_effect=[broken, healthy])
    sleeps = []
    client = ReconnectingClient(factory, sleep=sleeps.append)
    assert client.call("get_object", Bucket="b", Key="k") == {"ok": True}
    assert factory.call_count == 2
    assert sleeps == [2.0]


def test_reconnect_gives_up_after_max_attempts():
    broken = MagicMock()
    broken.put_object.side_effect = ConnectionClosedError(endpoint_url="http://s3")
    client = ReconnectingClient(lambda: broken, sleep=lambda _: None)
    with pytest.raises(ConnectionClosedError):
        client.call("put_object", Bucket="b", Key="k", Body=b"")
    assert broken.put_object.call_count == 3


# -- settings / client config (LDR-0008) -------------------------------------------


ENV = {"S3_ACCESSKEY": "ak", "S3_SECRET": "sk", "S3_ENDPOINT": "https://s3.internal", "REGION_NAME": "us-east-1"}


def test_settings_from_env_defaults():
    s = S3Settings.from_env(ENV | {"CFR_ENV": "qa"})
    assert s.bucket == "cfr-v2-qa-artifacts"
    assert s.restricted_bucket == "cfr-v2-qa-artifacts-restricted"
    assert s.credentials_for(Classification.RESTRICTED) == S3Credentials("ak", "sk")


def test_settings_restricted_credentials_and_overrides():
    s = S3Settings.from_env(
        ENV
        | {"S3_RESTRICTED_ACCESSKEY": "rak", "S3_RESTRICTED_SECRET": "rsk", "CFR_ARTIFACT_BUCKET": "custom"}
    )
    assert s.credentials_for(Classification.RESTRICTED) == S3Credentials("rak", "rsk")
    assert s.credentials_for(Classification.INTERNAL) == S3Credentials("ak", "sk")
    assert s.bucket == "custom"


def test_client_config_matches_v1_pattern():
    cfg = client_config()
    assert cfg.s3 == {"addressing_style": "path"}
    assert cfg.signature_version == "s3v4"
    assert cfg.retries["mode"] == "standard"


@pytest.mark.parametrize("ca_bundle, expected", [(None, True), ("/etc/ssl/corp.pem", "/etc/ssl/corp.pem")])
def test_tls_verification_never_disabled(monkeypatch, ca_bundle, expected):
    captured = {}
    monkeypatch.setattr("cfr_v2.artifacts.s3.boto3.client", lambda *a, **kw: captured.update(kw))
    from cfr_v2.artifacts.s3 import make_client

    make_client(_settings(ca_bundle=ca_bundle), S3Credentials("a", "b"))
    assert captured["verify"] == expected
    assert captured["verify"] is not False
