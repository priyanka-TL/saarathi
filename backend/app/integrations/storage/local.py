"""Filesystem object store, for development.

WHY THIS EXISTS. Voice is the first feature in this app that needs a bucket, and
without a local driver every developer would need cloud credentials before they
could see a microphone work at all. This satisfies the same interface with a
directory, so `CLOUD_STORAGE_PROVIDER=local` runs the full record -> transcribe
-> speak cycle offline. Mitra has the same escape hatch
(`chatbot/services/storage/local_storage_handler.py` plus a
`PUT /api/storage/upload-local/<key>` route); this is that idea, tightened.

"PRESIGNED" URLS POINT BACK AT THIS SERVICE. There is no storage origin to
upload to, so `presign_put` returns a URL for `PUT {prefix}/api/voice/upload-local/{key}`
carrying an HMAC of the key and an expiry. The route is otherwise unauthenticated
-- it has to be, because a presigned URL is used by a bare `fetch` with no
Authorization header -- so without the signature it would be an open
write-anything-anywhere endpoint on a developer's machine.

The signing key is random per process and held only in memory: restarting the
backend invalidates outstanding URLs, which is correct for something that lives
five minutes and costs nothing to reissue.

DEV ONLY. No lifecycle expiry, no concurrent-writer story, no durability. Do not
set CLOUD_STORAGE_PROVIDER=local in a deployment.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from app.integrations.storage.base import ObjectStore, PresignedUpload
from app.integrations.storage.exceptions import (
    StorageConfigError,
    StorageDownloadError,
    StorageTooLargeError,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[3]


class LocalObjectStore(ObjectStore):
    provider = "local"

    def __init__(self, settings: Any) -> None:
        root = Path((settings.local_storage_dir or "var/storage").strip())
        # Relative paths resolve against backend/, not the CWD -- the same rule
        # .env follows, so `make run` from anywhere behaves identically.
        self.root = (root if root.is_absolute() else BASE_DIR / root).resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageConfigError(
                f"LOCAL_STORAGE_DIR {self.root} could not be created: {exc}"
            ) from exc

        # CLOUD_ENDPOINT means "where the storage service lives", which for this
        # driver is this app. Without it, derive from HOST/PORT: 0.0.0.0 is a
        # bind address, not somewhere a browser can connect to.
        endpoint = (settings.cloud_endpoint or "").strip().rstrip("/")
        if not endpoint:
            host = settings.host if settings.host not in ("0.0.0.0", "::") else "127.0.0.1"
            endpoint = f"http://{host}:{settings.port}"
        elif "://" not in endpoint:
            endpoint = f"http://{endpoint}"
        self._upload_base = f"{endpoint}{settings.api_prefix}/api/voice/upload-local"

        # Per-process and never persisted; see the module docstring.
        self._signing_key = secrets.token_bytes(32)
        logger.info("storage[local] ready  root=%s  upload_base=%s", self.root, self._upload_base)

    # ---- signing ----------------------------------------------------------

    def sign(self, key: str, expires_at: int) -> str:
        return hmac.new(
            self._signing_key, f"{key}:{expires_at}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def verify(self, key: str, expires_at: int, signature: str) -> bool:
        if expires_at < int(time.time()):
            return False
        # compare_digest, not ==, so a wrong signature cannot be recovered one
        # byte at a time from response timing.
        return hmac.compare_digest(self.sign(key, expires_at), signature)

    # ---- ObjectStore ------------------------------------------------------

    def presign_put(
        self, key: str, content_type: str, expires_s: int = 300
    ) -> PresignedUpload:
        expires_at = int(time.time()) + expires_s
        signature = self.sign(key, expires_at)
        url = (
            f"{self._upload_base}/{quote(key)}"
            f"?expires={expires_at}&signature={signature}"
        )
        return PresignedUpload(url=url, key=key, method="PUT", expires_in_s=expires_s)

    def public_url(self, key: str) -> str:
        # "Public" is meaningless on a filesystem -- there is no anonymous
        # reader to grant access to -- so this is the signed URL either way.
        # Returning something unsigned would be a lie that only shows up as a
        # 403 much later.
        return self.download_url(key)

    def download_url(self, key: str, expires_s: int = 600) -> str:
        # Nothing reads objects through a URL in this app -- the backend fetches
        # by key. Kept so the interface is total rather than partly implemented.
        expires_at = int(time.time()) + expires_s
        return (
            f"{self._upload_base}/{quote(key)}"
            f"?expires={expires_at}&signature={self.sign(key, expires_at)}"
        )

    def fetch(self, key: str, max_bytes: int) -> bytes:
        path = self._resolve(key)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc
        if size > max_bytes:
            raise StorageTooLargeError(key, size, max_bytes)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageDownloadError(key, type(exc).__name__) from exc

    def delete(self, key: str) -> None:
        try:
            self._resolve(key).unlink(missing_ok=True)
        except (OSError, StorageDownloadError) as exc:
            logger.warning("storage[local]: delete failed for %s (%s)", key, type(exc).__name__)

    # ---- write path, used by the upload-local route -----------------------

    def put(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename, so a reader never sees a partial file: fetch() runs
        # immediately after the upload and a torn read would look like corrupt
        # audio rather than a race.
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    # ---- helpers ----------------------------------------------------------

    def _resolve(self, key: str) -> Path:
        """Map a key to a path, refusing anything that escapes the root.

        Keys are server-generated, so traversal should be impossible upstream --
        but this driver also backs a route that takes the key from a URL, and a
        containment check next to the filesystem call is the one that cannot be
        bypassed by a future caller who forgets.
        """
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageDownloadError(key, "key escapes the storage root")
        return candidate

    def size_of(self, key: str) -> Optional[int]:
        try:
            return self._resolve(key).stat().st_size
        except (OSError, StorageDownloadError):
            return None
