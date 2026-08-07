"""Google Cloud Storage.

Responsible for: V4 signed URLs, fetch and delete against GCS.
Used by: selected by factory.py when CLOUD_STORAGE_PROVIDER is gcp/gcs/google.

THE ELEVATE ENV CONVENTION HAS NO PROJECT SLOT, so CLOUD_STORAGE_REGION carries
the PROJECT ID here, and CLOUD_STORAGE_SECRET is a whole service-account JSON.
Getting the project wrong fails at BOOT with "Project was not passed and could
not be determined".
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Dict
from urllib.parse import quote

from app.integrations.storage.base import ObjectStore, PresignedUpload
from app.integrations.storage.exceptions import (
    StorageConfigError,
    StorageDownloadError,
    StorageTooLargeError,
    StorageUploadError,
)

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"


class GcsObjectStore(ObjectStore):
    provider = "gcp"

    def __init__(self, settings: Any) -> None:
        try:
            from google.cloud import storage as gcs
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise StorageConfigError(
                "CLOUD_STORAGE_PROVIDER=gcp requires google-cloud-storage. "
                "Install it with `uv pip install google-cloud-storage`, or set "
                "CLOUD_STORAGE_PROVIDER to aws or local."
            ) from exc

        bucket_name = (settings.cloud_storage_bucketname or "").strip()
        if not bucket_name:
            raise StorageConfigError(
                "CLOUD_STORAGE_BUCKETNAME is required when CLOUD_STORAGE_PROVIDER=gcp."
            )

        info = self._credentials_info(settings)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[_SCOPE]
        )
        # project MUST be resolvable. Passing None makes the client try to infer
        # one from the environment and raise "Project was not passed and could
        # not be determined" -- at BOOT, before anything has a chance to work.
        # The service-account JSON usually carries it; the bare-key path takes
        # it from CLOUD_STORAGE_REGION (see _credentials_info).
        client = gcs.Client(project=info.get("project_id"), credentials=credentials)
        # bucket() is a local handle -- no network call, so a wrong name surfaces
        # on first use rather than at boot. That is deliberate: a boot that
        # depends on a reachable bucket cannot start during a GCS incident.
        self._bucket = client.bucket(bucket_name)
        self.bucket = bucket_name
        self.bucket_type = (settings.cloud_storage_bucket_type or "private").strip().lower()
        logger.info(
            "storage[gcp] ready  bucket=%s  type=%s  project=%s",
            bucket_name,
            self.bucket_type,
            info.get("project_id"),
        )

    @staticmethod
    def _credentials_info(settings: Any) -> Dict[str, Any]:
        """Parse CLOUD_STORAGE_SECRET into service-account credentials.

        Two accepted shapes, because the shared env convention has one SECRET
        slot and GCP needs more than one field:

          * the full service-account JSON  (preferred)
          * a bare private key, with CLOUD_STORAGE_ACCOUNTNAME as the email
            and CLOUD_STORAGE_REGION as the PROJECT ID

        That last mapping looks wrong and is not: the convention has no project
        slot, so REGION carries it under GCP -- the same one-name-two-meanings
        trade `ACCOUNTNAME` and `SECRET` already make. It is what the ELEVATE
        services do, and what an existing `.env` will contain.
        """
        secret = (settings.cloud_storage_secret or "").strip()
        if not secret:
            raise StorageConfigError(
                "CLOUD_STORAGE_SECRET is required when CLOUD_STORAGE_PROVIDER=gcp. "
                "Provide the full service-account JSON."
            )

        if secret.startswith("{"):
            try:
                info = json.loads(secret)
            except json.JSONDecodeError as exc:
                # The message must not include `secret` -- it is the credential.
                raise StorageConfigError(
                    "CLOUD_STORAGE_SECRET looks like JSON but failed to parse "
                    f"({exc.msg} at position {exc.pos}). Check that it was not "
                    "truncated or shell-mangled when it was set."
                ) from exc
            if "private_key" in info:
                # Env serialisation turns real newlines into the two characters
                # \ and n; the key is unusable until they are turned back.
                info["private_key"] = str(info["private_key"]).replace("\\n", "\n")
            return info

        email = (settings.cloud_storage_accountname or "").strip()
        if not email:
            raise StorageConfigError(
                "CLOUD_STORAGE_SECRET holds a bare private key, so "
                "CLOUD_STORAGE_ACCOUNTNAME must be the service-account email."
            )
        project = (settings.cloud_storage_region or "").strip()
        if not project:
            raise StorageConfigError(
                "CLOUD_STORAGE_SECRET holds a bare private key, so "
                "CLOUD_STORAGE_REGION must be the GCP project id (the convention "
                "has no separate project slot). Or supply the full "
                "service-account JSON, which carries project_id itself."
            )
        return {
            "type": "service_account",
            "private_key": secret.replace("\\n", "\n"),
            "client_email": email,
            "project_id": project,
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    def presign_put(
        self, key: str, content_type: str, expires_s: int = 300
    ) -> PresignedUpload:
        # content_type is NOT signed -- same reason as the S3 driver. GCS is
        # stricter still: a signed Content-Type must match byte for byte.
        try:
            url = self._bucket.blob(key).generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expires_s),
                method="PUT",
            )
        except Exception as exc:
            raise StorageUploadError(key, type(exc).__name__) from exc
        return PresignedUpload(url=url, key=key, method="PUT", expires_in_s=expires_s)

    def public_url(self, key: str) -> str:
        # GCS has no per-bucket hostname the way S3 does; the JSON/XML media
        # endpoint is the stable public address.
        return f"https://storage.googleapis.com/{self.bucket}/{quote(key)}"

    def download_url(self, key: str, expires_s: int = 600) -> str:
        if self.bucket_type == "public":
            return self.public_url(key)
        try:
            return self._bucket.blob(key).generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expires_s),
                method="GET",
            )
        except Exception as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc

    def fetch(self, key: str, max_bytes: int) -> bytes:
        try:
            blob = self._bucket.blob(key)
            # A fresh handle has size=None until it is populated from the API.
            blob.reload()
        except Exception as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc

        if blob.size is not None and blob.size > max_bytes:
            raise StorageTooLargeError(key, blob.size, max_bytes)

        try:
            # Ranged read as the backstop for a missing/incorrect size, matching
            # the S3 driver: one byte past the limit proves an overrun.
            data = blob.download_as_bytes(start=0, end=max_bytes)
        except Exception as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc

        if len(data) > max_bytes:
            raise StorageTooLargeError(key, None, max_bytes)
        return data

    def delete(self, key: str) -> None:
        try:
            self._bucket.blob(key).delete()
        except Exception as exc:
            logger.warning("storage[gcp]: delete failed for %s (%s)", key, type(exc).__name__)
