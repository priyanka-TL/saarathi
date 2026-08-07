"""The object-storage abstraction: factory resolution and the local driver.

Nothing here touches a network. The suite runs with `--disable-socket`, and the
AWS driver is exercised against a stubbed boto3 client rather than S3 -- what is
being pinned is the CONTRACT (keys not URLs, size checked before the body,
containment enforced), not Amazon's behaviour.

Settings is constructed with explicit kwargs, so these assertions pin behaviour
rather than whatever the developer's .env currently says.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.core.settings import Settings
from app.integrations.storage import (
    StorageConfigError,
    StorageDownloadError,
    StorageTooLargeError,
    get_object_store,
    supported_providers,
)
from app.integrations.storage.local import LocalObjectStore


def _settings(**overrides) -> Settings:
    return Settings(OPENROUTER_API_KEY="test", **overrides)


def _aws_settings(**overrides) -> Settings:
    """AWS settings with explicit credentials.

    Not incidental: with no credentials the driver falls back to the IAM role
    chain, and botocore resolves that against the instance-metadata service
    while the client is being CONSTRUCTED. Under `--disable-socket` that is a
    hard failure, so every AWS test states its credentials rather than
    depending on the machine it runs on.
    """
    return _settings(
        **{
            "cloud_storage_provider": "aws",
            "cloud_storage_bucketname": "bucket",
            "cloud_storage_region": "ap-south-1",
            "cloud_storage_accountname": "AKIAEXAMPLE",
            "cloud_storage_secret": "secret",
            **overrides,
        }
    )


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alias", ["aws", "s3", "oci", "minio"])
def test_s3_aliases_share_one_driver(alias, tmp_path):
    """The protocol is the interface. CLOUD_ENDPOINT distinguishes the vendors,
    so all four names must land on the same class rather than on stubs."""
    store = get_object_store(_aws_settings(cloud_storage_provider=alias))
    assert type(store).__name__ == "S3ObjectStore"


def test_local_provider_resolves(tmp_path):
    store = get_object_store(
        _settings(cloud_storage_provider="local", local_storage_dir=str(tmp_path))
    )
    assert isinstance(store, LocalObjectStore)
    assert store.provider == "local"


def test_provider_is_case_and_whitespace_insensitive(tmp_path):
    store = get_object_store(
        _settings(cloud_storage_provider="  LOCAL  ", local_storage_dir=str(tmp_path))
    )
    assert isinstance(store, LocalObjectStore)


def test_unknown_provider_names_the_supported_set():
    """A typo must say what the options are. This is a BOOT failure, so the
    message is the only thing the operator gets."""
    with pytest.raises(StorageConfigError) as exc:
        get_object_store(_settings(cloud_storage_provider="nonsense"))
    message = str(exc.value)
    assert "nonsense" in message
    for provider in supported_providers():
        assert provider in message


def test_empty_provider_is_rejected():
    with pytest.raises(StorageConfigError):
        get_object_store(_settings(cloud_storage_provider=""))


def test_s3_without_a_bucket_fails_at_boot():
    """Deliberately fatal: a missing bucket found on the first recording is an
    outage mid-turn, the same mistake at startup is a failed deploy."""
    with pytest.raises(StorageConfigError) as exc:
        get_object_store(_aws_settings(cloud_storage_bucketname=""))
    assert "CLOUD_STORAGE_BUCKETNAME" in str(exc.value)


def test_repr_cannot_leak_the_secret():
    """__repr__ is overridden because the default prints instance attributes,
    and one of them is a client built from CLOUD_STORAGE_SECRET."""
    store = get_object_store(_aws_settings(cloud_storage_secret="super-secret-value"))
    assert "super-secret-value" not in repr(store)
    assert "AKIAEXAMPLE" not in repr(store)


# ---------------------------------------------------------------------------
# local driver
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_store(tmp_path) -> LocalObjectStore:
    return get_object_store(
        _settings(
            cloud_storage_provider="local",
            local_storage_dir=str(tmp_path),
            host="127.0.0.1",
            port=8000,
            api_prefix="/saarathi-service",
        )
    )


def test_local_round_trip(local_store):
    local_store.put("voice/abc/clip.webm", b"audio-bytes")
    assert local_store.fetch("voice/abc/clip.webm", max_bytes=1024) == b"audio-bytes"


def test_local_fetch_rejects_oversized_object(local_store):
    local_store.put("voice/abc/clip.webm", b"x" * 100)
    with pytest.raises(StorageTooLargeError):
        local_store.fetch("voice/abc/clip.webm", max_bytes=10)


def test_local_fetch_missing_key(local_store):
    with pytest.raises(StorageDownloadError):
        local_store.fetch("voice/abc/nope.webm", max_bytes=1024)


def test_local_delete_is_idempotent(local_store):
    local_store.put("voice/abc/clip.webm", b"x")
    local_store.delete("voice/abc/clip.webm")
    local_store.delete("voice/abc/clip.webm")  # must not raise


@pytest.mark.parametrize(
    "key",
    ["../escape.txt", "voice/../../escape.txt", "/etc/passwd", "voice/./../../x"],
)
def test_local_refuses_keys_that_escape_the_root(local_store, key):
    """Keys are server-generated, so this should be unreachable -- but this
    driver also backs a route that reads the key out of a URL, and the check
    next to the filesystem call is the one a future caller cannot forget."""
    with pytest.raises(StorageDownloadError):
        local_store.fetch(key, max_bytes=1024)


def test_local_public_url_is_still_signed(local_store):
    """"Public" is meaningless on a filesystem -- there is no anonymous reader
    to grant access to. Returning something unsigned would be a lie that only
    surfaces as a 403 much later."""
    assert "signature=" in local_store.public_url("voice/abc/clip.webm")


def test_local_presign_is_absolute_and_carries_a_signature(local_store):
    presigned = local_store.presign_put("voice/abc/clip.webm", "audio/webm", expires_s=300)
    assert presigned.method == "PUT"
    assert presigned.key == "voice/abc/clip.webm"
    # Absolute: the frontend runs on :5173 and a relative URL would hit Vite.
    assert presigned.url.startswith("http://127.0.0.1:8000/saarathi-service/api/voice/upload-local/")
    assert "signature=" in presigned.url and "expires=" in presigned.url


def test_local_presign_honours_cloud_endpoint(tmp_path):
    """CLOUD_ENDPOINT means 'where the storage service lives', which for this
    driver is this app -- so it must override the HOST/PORT derivation."""
    store = get_object_store(
        _settings(
            cloud_storage_provider="local",
            local_storage_dir=str(tmp_path),
            cloud_endpoint="http://saarthi.internal:9000",
            api_prefix="",
        )
    )
    assert store.presign_put("k", "audio/webm").url.startswith(
        "http://saarthi.internal:9000/api/voice/upload-local/k"
    )


def test_local_signature_verification(local_store):
    expires = int(time.time()) + 300
    signature = local_store.sign("voice/abc/clip.webm", expires)

    assert local_store.verify("voice/abc/clip.webm", expires, signature)
    # Wrong key, wrong expiry, wrong signature: all rejected.
    assert not local_store.verify("voice/other/clip.webm", expires, signature)
    assert not local_store.verify("voice/abc/clip.webm", expires + 1, signature)
    assert not local_store.verify("voice/abc/clip.webm", expires, "deadbeef")


def test_local_signature_expires(local_store):
    """Without this the local route is an open write-anything endpoint: a
    presigned URL is used by a bare fetch with no Authorization header."""
    expired = int(time.time()) - 1
    assert not local_store.verify("k", expired, local_store.sign("k", expired))


def test_local_signing_key_is_per_process(tmp_path):
    """Held only in memory, so a restart invalidates outstanding URLs. Correct
    for something that lives five minutes and costs nothing to reissue."""
    settings = _settings(cloud_storage_provider="local", local_storage_dir=str(tmp_path))
    first, second = get_object_store(settings), get_object_store(settings)
    expires = int(time.time()) + 300
    assert first.sign("k", expires) != second.sign("k", expires)


# ---------------------------------------------------------------------------
# aws driver -- stubbed client, no network
# ---------------------------------------------------------------------------

class _FakeS3:
    """The three boto3 calls the driver makes, and nothing else."""

    def __init__(self, *, body=b"", length=None, head_raises=False):
        self._body, self._length, self._head_raises = body, length, head_raises
        self.deleted: list[str] = []

    def generate_presigned_url(self, operation, Params, ExpiresIn, HttpMethod):
        return f"https://s3.example/{Params['Key']}?op={operation}&exp={ExpiresIn}"

    def head_object(self, Bucket, Key):
        if self._head_raises:
            raise RuntimeError("no such key")
        return {} if self._length is None else {"ContentLength": self._length}

    def get_object(self, Bucket, Key):
        data = self._body

        class _Body:
            def read(self, n):
                return data[:n]

        return {"Body": _Body()}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)


def _s3_store(fake) -> "object":
    store = get_object_store(_aws_settings())
    store._client = fake
    return store


def test_s3_presign_put_does_not_sign_content_type():
    """Signing Content-Type breaks the upload whenever the browser sends
    something slightly different -- and browsers vary the codecs parameter on
    audio/webm by version and platform."""
    fake = _FakeS3()
    captured = {}

    def _capture(operation, Params, ExpiresIn, HttpMethod):
        captured.update(Params)
        return "https://s3.example/signed"

    fake.generate_presigned_url = _capture
    _s3_store(fake).presign_put("voice/a/clip.webm", "audio/webm;codecs=opus")

    assert set(captured) == {"Bucket", "Key"}
    assert "ContentType" not in captured


def test_s3_fetch_rejects_oversize_from_head_without_downloading():
    """The point of the metadata precheck: an oversized upload costs a HEAD,
    not a download into memory."""
    fake = _FakeS3(body=b"x" * 5000, length=5000)
    fake.get_object = lambda **kw: pytest.fail("body must not be read after a HEAD overrun")
    with pytest.raises(StorageTooLargeError):
        _s3_store(fake).fetch("k", max_bytes=100)


def test_s3_fetch_bounds_the_read_when_head_reports_no_length():
    """A bucket that does not report a length (or lies) still must not be able
    to stream unbounded data into memory."""
    fake = _FakeS3(body=b"x" * 5000, length=None)
    with pytest.raises(StorageTooLargeError):
        _s3_store(fake).fetch("k", max_bytes=100)


def test_s3_fetch_returns_bytes_within_the_limit():
    assert _s3_store(_FakeS3(body=b"hello", length=5)).fetch("k", max_bytes=100) == b"hello"


def test_s3_fetch_wraps_upstream_failures():
    """Callers must be able to catch storage failures without importing
    botocore -- that is what keeps the router provider-agnostic."""
    with pytest.raises(StorageDownloadError):
        _s3_store(_FakeS3(head_raises=True)).fetch("k", max_bytes=100)


def test_s3_delete_never_raises():
    """Best-effort: the bucket lifecycle rule is the real guarantee, so a failed
    cleanup must not cost the caller their answer."""
    fake = _FakeS3()
    fake.delete_object = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    _s3_store(fake).delete("k")  # must not raise


# ---------------------------------------------------------------------------
# bucket type -- private vs public
#
# The whole surface of CLOUD_STORAGE_BUCKET_TYPE. It changes how objects are
# READ and nothing else; the write path stays signed on both, which is the
# property most worth pinning here.
# ---------------------------------------------------------------------------

def _gcp_settings(**overrides) -> Settings:
    """GCP with a bare private key, the shape the ELEVATE .env files use."""
    return _settings(
        **{
            "cloud_storage_provider": "gcp",
            "cloud_storage_bucketname": "gcs-bucket",
            "cloud_storage_accountname": "sa@proj.iam.gserviceaccount.com",
            # Not a real key -- these tests never construct a google client.
            "cloud_storage_secret": "-----BEGIN PRIVATE KEY-----\\nAAAA\\n-----END PRIVATE KEY-----\\n",
            "cloud_storage_region": "my-project",
            **overrides,
        }
    )


@pytest.mark.parametrize("bucket_type", ["private", "public"])
def test_uploads_are_signed_on_both_bucket_types(bucket_type):
    """A bucket being publicly READABLE is never a reason to accept anonymous
    writes -- that would let anyone put anything at any key."""
    store = _s3_store(_FakeS3())
    store.bucket_type = bucket_type
    url = store.presign_put("voice/a/clip.webm", "audio/webm").url
    # The fake echoes the operation it was asked to sign, so seeing put_object
    # proves the request went through generate_presigned_url rather than being
    # short-circuited to a plain public URL the way reads are.
    assert "op=put_object" in url


def test_s3_download_url_is_signed_when_private():
    store = _s3_store(_FakeS3())
    store.bucket_type = "private"
    url = store.download_url("voice/a/clip.webm", expires_s=600)
    assert "op=get_object" in url and "exp=600" in url


def test_s3_download_url_is_plain_and_stable_when_public():
    store = _s3_store(_FakeS3())
    store.bucket_type = "public"
    url = store.download_url("voice/a/clip.webm", expires_s=600)

    assert url == "https://bucket.s3.ap-south-1.amazonaws.com/voice/a/clip.webm"
    # No signature and no expiry: two calls a second apart must agree, which is
    # what "stable" means and what a signed URL cannot give you.
    assert url == store.download_url("voice/a/clip.webm", expires_s=1)
    assert "Signature" not in url and "exp" not in url


def test_s3_public_url_uses_the_regional_endpoint():
    """A bucket outside us-east-1 answers the legacy global form with a 307,
    and not every client follows it."""
    store = _s3_store(_FakeS3())
    assert store.public_url("k").startswith("https://bucket.s3.ap-south-1.amazonaws.com/")


def test_s3_public_url_is_path_style_behind_a_custom_endpoint():
    """Virtual-host style would need the bucket as a DNS label on that host,
    which OCI and MinIO do not generally provide."""
    store = get_object_store(_aws_settings(cloud_endpoint="https://minio.local:9000"))
    store._client = _FakeS3()
    assert store.public_url("voice/a/c.webm") == "https://minio.local:9000/bucket/voice/a/c.webm"


def test_public_urls_escape_the_key():
    store = _s3_store(_FakeS3())
    assert " " not in store.public_url("voice/a b/c.webm")


def test_gcs_public_url_shape():
    from app.integrations.storage.gcp import GcsObjectStore

    store = GcsObjectStore.__new__(GcsObjectStore)   # no google client needed
    store.bucket = "gcs-bucket"
    store.bucket_type = "public"
    assert store.public_url("voice/a/c.webm") == "https://storage.googleapis.com/gcs-bucket/voice/a/c.webm"
    assert store.download_url("voice/a/c.webm") == store.public_url("voice/a/c.webm")


# ---------------------------------------------------------------------------
# bucket-type validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [("private", "private"), ("PUBLIC", "public"), ("  Public ", "public")])
def test_bucket_type_is_normalised(raw, expected):
    assert _settings(cloud_storage_bucket_type=raw).cloud_storage_bucket_type == expected


@pytest.mark.parametrize("bad", ["privat", "world-readable", "true", ""])
def test_bucket_type_rejects_anything_else(bad):
    """Rejected rather than defaulted. Falling back to 'private' would be the
    safe direction, but a validator that only catches one direction is worse
    than one that catches both."""
    with pytest.raises(Exception) as exc:
        _settings(cloud_storage_bucket_type=bad)
    assert "CLOUD_STORAGE_BUCKET_TYPE" in str(exc.value)


# ---------------------------------------------------------------------------
# ELEVATE .env compatibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alias", ["gcp", "gcs", "gcloud", "google"])
def test_gcp_aliases_all_resolve(alias):
    """`gcloud` is what the ELEVATE Node services actually write. Accepting it
    is most of what 'an ELEVATE .env drops in unchanged' means."""
    from app.integrations.storage.factory import _STORES

    assert _STORES[alias] == ("app.integrations.storage.gcp", "GcsObjectStore")


def test_bare_cloud_storage_key_is_a_fallback_for_the_provider(tmp_path):
    """Some ELEVATE .env files set only CLOUD_STORAGE."""
    store = get_object_store(
        _settings(cloud_storage_provider="", cloud_storage="local", local_storage_dir=str(tmp_path))
    )
    assert isinstance(store, LocalObjectStore)


def test_azure_is_not_advertised_as_supported():
    """A name in the registry is a promise supported_providers() prints back to
    the operator; pointing one at a missing module would turn a clear
    'unsupported provider' message into a ModuleNotFoundError at boot."""
    assert "azure" not in supported_providers()


# ---------------------------------------------------------------------------
# GCP credential assembly -- no network, no google client
# ---------------------------------------------------------------------------

def test_gcp_takes_the_project_id_from_cloud_storage_region():
    """The convention has no project slot, so REGION carries it. Without this
    the client is built with project=None and raises 'Project was not passed
    and could not be determined' -- at BOOT."""
    from app.integrations.storage.gcp import GcsObjectStore

    info = GcsObjectStore._credentials_info(_gcp_settings())
    assert info["project_id"] == "my-project"
    assert info["client_email"] == "sa@proj.iam.gserviceaccount.com"
    assert info["type"] == "service_account"
    # Env serialisation turns real newlines into the two characters \ and n;
    # the key is unusable until they are turned back.
    assert "\\n" not in info["private_key"] and "\n" in info["private_key"]


def test_gcp_prefers_a_full_service_account_json():
    from app.integrations.storage.gcp import GcsObjectStore

    info = GcsObjectStore._credentials_info(
        _gcp_settings(
            cloud_storage_secret='{"type":"service_account","project_id":"from-json",'
            '"client_email":"x@y.iam.gserviceaccount.com","private_key":"-----BEGIN K-----\\\\nAA\\\\n"}'
        )
    )
    assert info["project_id"] == "from-json"
    assert "\\n" not in info["private_key"]


def test_gcp_bare_key_without_a_project_fails_with_a_usable_message():
    from app.integrations.storage.gcp import GcsObjectStore

    with pytest.raises(StorageConfigError) as exc:
        GcsObjectStore._credentials_info(_gcp_settings(cloud_storage_region=""))
    assert "CLOUD_STORAGE_REGION" in str(exc.value)


def test_gcp_malformed_json_error_does_not_echo_the_secret():
    """CLOUD_STORAGE_SECRET is a credential; a parse error must not print it."""
    from app.integrations.storage.gcp import GcsObjectStore

    # A distinctive value: "SEC" would collide with the word CLOUD_STORAGE_SECRET
    # in the message and pass for the wrong reason.
    with pytest.raises(StorageConfigError) as exc:
        GcsObjectStore._credentials_info(
            _gcp_settings(cloud_storage_secret='{"private_key":"kM9zQvT7pLxW')
        )
    assert "kM9zQvT7pLxW" not in str(exc.value)
