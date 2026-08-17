"""Provider-agnostic object storage.

Import `get_object_store` and the exceptions from here; the concrete drivers
are an implementation detail and are loaded lazily by the factory.
"""
from app.integrations.storage.base import ObjectStore, PresignedUpload
from app.integrations.storage.exceptions import (
    StorageConfigError,
    StorageDownloadError,
    StorageError,
    StorageTooLargeError,
    StorageUploadError,
)
from app.integrations.storage.factory import get_object_store, supported_providers

__all__ = [
    "ObjectStore",
    "PresignedUpload",
    "StorageConfigError",
    "StorageDownloadError",
    "StorageError",
    "StorageTooLargeError",
    "StorageUploadError",
    "get_object_store",
    "supported_providers",
]
