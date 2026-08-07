"""S3-compatible object storage.

Responsible for: presigned PUT/GET, fetch and delete against S3, OCI or MinIO.
Used by: selected by factory.py when CLOUD_STORAGE_PROVIDER is aws/s3/oci/minio.

CLOUD_ENDPOINT is what distinguishes the three -- one boto3 driver serves them
all. boto3 resolves credentials while the client is being CONSTRUCTED, which is
why tests must state their own rather than falling through to the IMDS chain.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import quote

from app.integrations.storage.base import ObjectStore, PresignedUpload
from app.integrations.storage.exceptions import (
    StorageConfigError,
    StorageDownloadError,
    StorageTooLargeError,
    StorageUploadError,
)

logger = logging.getLogger(__name__)


class S3ObjectStore(ObjectStore):
    provider = "aws"

    def __init__(self, settings: Any) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise StorageConfigError(
                "CLOUD_STORAGE_PROVIDER is an S3-compatible provider but boto3 is "
                "not installed. Run `make install`."
            ) from exc

        self.bucket = (settings.cloud_storage_bucketname or "").strip()
        if not self.bucket:
            raise StorageConfigError(
                "CLOUD_STORAGE_BUCKETNAME is required when CLOUD_STORAGE_PROVIDER "
                f"is {settings.cloud_storage_provider!r}."
            )

        # Signature v4 is required for presigned URLs in every region created
        # after 2014, and `virtual` addressing is what AWS expects; a custom
        # endpoint (MinIO, some OCI setups) usually needs path addressing, so
        # that is switched with the endpoint.
        endpoint = (settings.cloud_endpoint or "").strip()
        kwargs: Dict[str, Any] = {
            "config": Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if endpoint else "virtual"},
                retries={
                    "max_attempts": settings.cloud_storage_max_attempts,
                    "mode": settings.cloud_storage_retry_mode,
                },
            )
        }

        access_key = (settings.cloud_storage_accountname or "").strip()
        secret_key = (settings.cloud_storage_secret or "").strip()
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
            logger.debug("storage[aws]: using explicit access-key credentials")
        elif access_key or secret_key:
            logger.warning(
                "storage[aws]: only one of CLOUD_STORAGE_ACCOUNTNAME / "
                "CLOUD_STORAGE_SECRET is set. Ignoring both and falling back to "
                "the IAM role / environment credential chain."
            )
        else:
            logger.info(
                "storage[aws]: no explicit credentials -- using the IAM role / "
                "environment credential chain."
            )

        region = (settings.cloud_storage_region or "").strip()
        if region:
            kwargs["region_name"] = region
        if endpoint:
            # boto3 wants a scheme. The convention's example value
            # ("s3.ap-south-1.amazonaws.com") has none, so add one rather than
            # failing on a value copied verbatim from the shared template.
            if "://" not in endpoint:
                endpoint = f"https://{endpoint}"
            kwargs["endpoint_url"] = endpoint

        self._client = boto3.client("s3", **kwargs)
        self._region = region
        self._endpoint = endpoint
        self.bucket_type = (settings.cloud_storage_bucket_type or "private").strip().lower()
        logger.info(
            "storage[aws] ready  bucket=%s  type=%s  region=%s  endpoint=%s",
            self.bucket,
            self.bucket_type,
            region or "(default)",
            endpoint or "(default)",
        )

    def presign_put(
        self, key: str, content_type: str, expires_s: int = 300
    ) -> PresignedUpload:
        # ContentType is deliberately absent from Params -- see the note in
        # ObjectStore.presign_put. Including it makes the browser's exact
        # Content-Type part of the signature, and browsers vary it.
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_s,
                HttpMethod="PUT",
            )
        except Exception as exc:
            raise StorageUploadError(key, type(exc).__name__) from exc
        return PresignedUpload(url=url, key=key, method="PUT", expires_in_s=expires_s)

    def public_url(self, key: str) -> str:
        if self._endpoint:
            # Path-style against a custom endpoint. Virtual-host style would
            # need the bucket as a DNS label on that host, which OCI and MinIO
            # do not generally provide.
            return f"{self._endpoint}/{self.bucket}/{quote(key)}"
        # Regional endpoint, not the legacy global one: a bucket outside
        # us-east-1 answers the global form with a 307 to this address anyway,
        # and some clients do not follow it.
        region = self._region or "us-east-1"
        return f"https://{self.bucket}.s3.{region}.amazonaws.com/{quote(key)}"

    def download_url(self, key: str, expires_s: int = 600) -> str:
        if self.bucket_type == "public":
            return self.public_url(key)
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_s,
                HttpMethod="GET",
            )
        except Exception as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc

    def fetch(self, key: str, max_bytes: int) -> bytes:
        size = self._content_length(key)
        if size is not None and size > max_bytes:
            raise StorageTooLargeError(key, size, max_bytes)

        try:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
            # One byte past the limit, so a bucket that did not report a length
            # (or lied about it) still cannot stream unbounded data into memory.
            data = body.read(max_bytes + 1)
        except (StorageTooLargeError, StorageDownloadError):
            raise
        except Exception as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc

        if len(data) > max_bytes:
            raise StorageTooLargeError(key, None, max_bytes)
        return data

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            # Best-effort: a recording that outlives its turn is collected by
            # the bucket lifecycle rule, so failing the caller's request over
            # it would trade a real answer for a cleanup detail.
            logger.warning("storage[aws]: delete failed for %s (%s)", key, type(exc).__name__)

    def _content_length(self, key: str) -> Optional[int]:
        """Object size from HEAD, or None when the bucket does not report one."""
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key).get("ContentLength")
        except Exception as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc
