"""Resolve CLOUD_STORAGE_PROVIDER to a driver.

Modelled on Mitra's `StorageFactory` (`chatbot/services/storage/storage_factory.py`),
which keeps a name -> handler registry so a provider can be added without
touching a caller. Adding Azure here is one class plus one line in `_STORES`.

IMPORTS ARE LAZY, INSIDE THE BRANCH. A deployment running AWS never imports
`google.cloud.storage`, which is what lets that package stay an optional
dependency rather than something every install pays for. It also means a
provider whose SDK is missing fails with a message naming the provider and the
fix, instead of an ImportError at module load that takes the whole app down
regardless of which provider was configured.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, Tuple

from app.integrations.storage.base import ObjectStore
from app.integrations.storage.exceptions import StorageConfigError

logger = logging.getLogger(__name__)

# provider name -> (module, class). The three S3 aliases share a driver: the
# protocol is the interface, and CLOUD_ENDPOINT is what distinguishes them.
_STORES: Dict[str, Tuple[str, str]] = {
    "aws": ("app.integrations.storage.aws", "S3ObjectStore"),
    "s3": ("app.integrations.storage.aws", "S3ObjectStore"),
    "oci": ("app.integrations.storage.aws", "S3ObjectStore"),
    "minio": ("app.integrations.storage.aws", "S3ObjectStore"),
    # `gcloud` is not a synonym anyone would invent -- it is what the ELEVATE
    # Node services actually write (see MAIN/workspace/project-service/.env),
    # and accepting it is most of what "an ELEVATE .env drops in unchanged"
    # means in practice.
    "gcp": ("app.integrations.storage.gcp", "GcsObjectStore"),
    "gcs": ("app.integrations.storage.gcp", "GcsObjectStore"),
    "gcloud": ("app.integrations.storage.gcp", "GcsObjectStore"),
    "google": ("app.integrations.storage.gcp", "GcsObjectStore"),
    "local": ("app.integrations.storage.local", "LocalObjectStore"),
    # Azure is deliberately absent rather than listed-but-unimplemented: a name
    # in this table is a promise `supported_providers()` prints back to the
    # operator, and pointing one at a missing module would turn a clear
    # "unsupported provider" message into a ModuleNotFoundError at boot.
}


def supported_providers() -> list[str]:
    return sorted(_STORES)


def get_object_store(settings: Any) -> ObjectStore:
    """Build the configured driver, or raise StorageConfigError.

    Called once at container build time, so this raises rather than returning
    None: a storage misconfiguration discovered on a user's first recording is
    an outage, whereas the same mistake at startup is a failed deploy.
    """
    name = (getattr(settings, "cloud_storage_provider", "") or "").strip().lower()
    if not name:
        # The ELEVATE services also accept a bare CLOUD_STORAGE key, and some
        # of their .env files set only that one. Falling back costs a line and
        # removes a confusing "not set" error for a file that plainly sets it.
        name = (getattr(settings, "cloud_storage", "") or "").strip().lower()
    if not name:
        raise StorageConfigError(
            "CLOUD_STORAGE_PROVIDER is not set. Supported providers: "
            + ", ".join(supported_providers())
        )

    target = _STORES.get(name)
    if target is None:
        raise StorageConfigError(
            f"Unsupported CLOUD_STORAGE_PROVIDER {name!r}. Supported providers: "
            + ", ".join(supported_providers())
        )

    module_path, class_name = target
    module = importlib.import_module(module_path)
    store: ObjectStore = getattr(module, class_name)(settings)
    logger.info("object store resolved: CLOUD_STORAGE_PROVIDER=%s -> %s", name, class_name)
    return store
