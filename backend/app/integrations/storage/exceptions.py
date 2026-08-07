"""Exceptions raised by the storage integration layer.

Thin wrappers so callers can catch storage failures without importing
`botocore.exceptions` or `google.api_core.exceptions` -- which is what keeps
the router provider-agnostic. A router that had to `except ClientError` would
be an AWS router with a GCP branch bolted on.

NOTHING HERE MAY CARRY A CREDENTIAL. `CLOUD_STORAGE_SECRET` is a service-account
JSON under gcp, and boto3/google exceptions are formatted into these messages by
callers -- so the drivers pass a short summary, never the raw upstream repr.
"""
from typing import Optional


class StorageError(Exception):
    """Base class for all object-storage errors."""


class StorageConfigError(StorageError):
    """Raised at BOOT when the storage configuration cannot produce a client.

    Deliberately fatal rather than deferred: a missing bucket name discovered on
    the first upload is an outage during a user's turn, whereas the same mistake
    caught at startup is a failed deploy. `build_container` lets it propagate.
    """


class StorageUploadError(StorageError):
    """Raised when a presigned upload URL cannot be generated."""

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(f"could not presign upload for {key!r}: {detail}")
        self.key = key


class StorageDownloadError(StorageError):
    """Raised when an object cannot be fetched.

    Covers "not found" as well as transport failures. The router maps both to
    the same answer on purpose -- telling a caller whether a key they guessed
    exists is an enumeration oracle over other users' recordings.
    """

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(f"could not fetch {key!r}: {detail}")
        self.key = key


class StorageTooLargeError(StorageError):
    """Raised when an object exceeds the caller's byte ceiling.

    Raised from the metadata precheck, BEFORE the body is buffered, so an
    oversized upload costs one HEAD rather than a download into memory.
    """

    def __init__(self, key: str, size: Optional[int], limit: int) -> None:
        actual = f"{size} bytes" if size is not None else "unknown size"
        super().__init__(f"object {key!r} is {actual}, over the {limit}-byte limit")
        self.key = key
        self.size = size
        self.limit = limit
