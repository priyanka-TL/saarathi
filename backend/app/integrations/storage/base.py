"""The ObjectStore interface.

Responsible for: the five operations every storage driver must provide.
Used by: VoiceService; drivers are selected by factory.py from configuration.

ADDRESSED BY KEY, NEVER BY URL. A key names an object inside a bucket fixed by
configuration, so there is no attacker-controlled destination and SSRF is
structurally impossible.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class PresignedUpload:
    """Everything the browser needs to perform the upload itself.

    The SERVER describes the request rather than the client assuming it, which
    is what lets one frontend code path drive an S3 presigned PUT, a GCS V4
    signed PUT and the local dev route without branching. `headers` is usually
    empty -- see the note on content_type in `ObjectStore.presign_put`.
    """

    url: str
    key: str
    method: str = "PUT"
    headers: Dict[str, str] = field(default_factory=dict)
    expires_in_s: int = 300


class ObjectStore(ABC):
    """Object storage over S3 / GCS / local disk."""

    #: Lowercase provider name, for logs and error messages. Set by each driver.
    provider: str = "unknown"

    #: "private" or "public", from CLOUD_STORAGE_BUCKET_TYPE. It changes how
    #: objects are READ and nothing else -- see `download_url`.
    bucket_type: str = "private"

    @abstractmethod
    def presign_put(
        self, key: str, content_type: str, expires_s: int = 300
    ) -> PresignedUpload:
        """Return a short-lived URL the browser can PUT the object to.

        ALWAYS SIGNED, ON BOTH BUCKET TYPES. A bucket being publicly READABLE
        is never a reason to accept anonymous writes -- that would let anyone
        put anything at any key. `bucket_type` deliberately has no effect here.

        CONTENT-TYPE IS NOT PART OF THE SIGNATURE. Signing it makes the upload
        fail whenever the browser sends something even slightly different from
        what was signed -- and browsers do exactly that, appending or dropping
        codec parameters on `audio/webm;codecs=opus` depending on version and
        platform. The parameter is accepted because drivers may still record it
        as object metadata, not because it is enforced.
        """

    @abstractmethod
    def download_url(self, key: str, expires_s: int = 600) -> str:
        """A URL a browser can read the object from.

        The one place `bucket_type` matters:

          private -> a signed URL that expires after `expires_s`
          public  -> the object's stable public URL: no signature, no expiry,
                     and `expires_s` is ignored because there is nothing to
                     expire

        Callers get a URL either way and do not branch on provider or bucket
        type, which is the point.
        """

    @abstractmethod
    def public_url(self, key: str) -> str:
        """The object's unsigned URL.

        Only resolvable by an anonymous caller when the bucket really is public
        -- this returns the address, it does not grant the access. Used by
        `download_url` under `bucket_type="public"`.
        """

    @abstractmethod
    def fetch(self, key: str, max_bytes: int) -> bytes:
        """Read an object into memory.

        Raises `StorageTooLargeError` if the object exceeds `max_bytes`,
        checked against metadata BEFORE the body is transferred, and
        `StorageDownloadError` for a missing object or a transport failure.
        """

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove an object. Deleting a missing key is not an error.

        Best-effort cleanup after a transcript is produced. The real guarantee
        that recordings do not accumulate is a bucket lifecycle rule, because
        this call does not run when a request fails midway.
        """

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Explicit, because the default would print instance attributes and one
        # of them is a client holding CLOUD_STORAGE_SECRET.
        return f"<{type(self).__name__} provider={self.provider}>"
