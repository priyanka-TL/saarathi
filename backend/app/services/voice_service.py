"""The voice workflow: presign an upload, transcribe a recording, speak a reply.

Responsible for: ordering the steps and owning the ownership check.
Used by: the voice router, via Depends(get_voice_service).

Raises StorageError / BhashiniError subtypes and lets the ROUTER map them to
status codes -- that is an HTTP decision and belongs in the API layer.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.domain.core import UserContext
from app.integrations.bhashini.exceptions import BhashiniTimeout, BhashiniUpstreamError
from app.integrations.bhashini.media_types import extension_for
from app.repositories.conversations import ConversationRepository
from app.utils.identifiers import parse_uuid

logger = get_logger(__name__)

#: Every object key this feature creates starts here.
KEY_PREFIX = "voice"

#: The languages Bhashini is wired up for.
SUPPORTED_LANGUAGES = ("en", "hi", "kn", "te")


class VoiceService:
    """One recording's journey, for one request.

    :param session: the request's DB session, for the ownership check.
    :param bhashini: the speech client. None when VOICE_ENABLED=0.
    :param object_store: the recording store. None when VOICE_ENABLED=0.
    :param settings: for the size ceiling and the presign TTL.
    """

    def __init__(self, session: Session, bhashini, object_store, settings) -> None:
        self._db = session
        self._bhashini = bhashini
        self._store = object_store
        self._settings = settings

    @property
    def enabled(self) -> bool:
        """Whether the full recording round-trip is available.

        Both or neither -- `build_container` builds them together, so one
        present and the other missing is not a state this can observe. Required
        by anything that touches the object store.
        """
        return self._bhashini is not None and self._store is not None

    @property
    def speech_enabled(self) -> bool:
        """Whether text-to-speech alone is available.

        Narrower than `enabled` on purpose: synthesis never touches the object
        store, and /api/voice/speak has always gated on the speech client alone.
        Widening it would make a storage misconfiguration silently take TTS down
        with it.
        """
        return self._bhashini is not None

    # ---- keys -------------------------------------------------------------

    def owned_key(self, raw: Any, user: UserContext) -> Optional[str]:
        """Validate an object key and confirm the caller owns its conversation.

        Keys are `voice/{conversation_id}/{uuid}.{ext}` and server-generated, so
        the shape check is cheap. The ownership check is the one that matters: a
        key is otherwise a read primitive over every recording in the bucket.

        Returns None for anything that fails, and the caller must answer 404 for
        ALL of them -- distinguishing "malformed", "not yours" and "no such
        conversation" would confirm which conversation ids exist.
        """
        if not isinstance(raw, str):
            return None
        key = raw.strip()
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != KEY_PREFIX:
            return None
        # No traversal, no absolute paths: every segment must be a plain name.
        # The local driver checks containment again next to the filesystem call,
        # but a caller should never get that far.
        if any(part in ("", ".", "..") or "\\" in part for part in parts):
            return None

        conversation_id = parse_uuid(parts[1])
        if conversation_id is None:
            return None
        if ConversationRepository(self._db).get_scoped(conversation_id, user) is None:
            return None
        return key

    def owns_conversation(self, conversation_id: uuid.UUID, user: UserContext) -> bool:
        return ConversationRepository(self._db).get_scoped(conversation_id, user) is not None

    # ---- workflow ---------------------------------------------------------

    def presign_upload(self, conversation_id: uuid.UUID, content_type: str):
        """A presigned direct-to-bucket PUT for one recording.

        THE KEY IS SERVER-GENERATED. A client-supplied key is an
        arbitrary-write primitive, and embedding the conversation id is what
        makes the ownership check on the way back in possible at all.

        :raises StorageError: the caller maps it to 502.
        """
        key = f"{KEY_PREFIX}/{conversation_id}/{uuid.uuid4().hex}.{extension_for(content_type)}"
        return self._store.presign_put(
            key, content_type, expires_s=self._settings.voice_upload_url_expiry_s,
        )

    def transcribe(self, key: str, language: str) -> str:
        """Fetch, transcribe, delete, and translate to English if needed.

        :raises StorageTooLargeError StorageDownloadError StorageError: on fetch.
        :raises BhashiniTimeout AudioConversionError BhashiniUpstreamError
            UnsupportedLanguageError: on transcription.
        :returns: the transcript, in English.
        """
        audio = self._store.fetch(key, max_bytes=self._settings.voice_max_audio_bytes)

        try:
            transcript = self._bhashini.transcribe(audio, language)
        finally:
            # Best effort, and OUTSIDE the success path on purpose: the recording
            # is PII and has no further use once transcribed. The bucket
            # lifecycle rule is the actual guarantee, because this does not run
            # when the process dies mid-request.
            self._store.delete(key)

        return self._to_english(transcript, language)

    def _to_english(self, transcript: str, language: str) -> str:
        """Translate to English, KEEPING THE SOURCE TEXT ON FAILURE.

        The LLM works in English, so an Indic recording is translated before it
        reaches the composer -- which is what keeps /api/chat monolingual and its
        contract unchanged.

        A failed translation returns the Indic transcript rather than nothing or
        an error: the user can still see and edit their own words, and losing
        them to a translation outage would be strictly worse than showing them
        untranslated.
        """
        if not transcript or language == "en":
            return transcript
        try:
            return self._bhashini.translate(transcript, language, "en")
        except (BhashiniUpstreamError, BhashiniTimeout) as exc:
            logger.warning(
                "voice: translation failed, returning source text (%s)", exc,
                extra={"language": language},
            )
            return transcript

    def synthesize(self, text: str, language: str) -> dict:
        """Speech for one agent reply.

        :raises BhashiniTimeout AudioConversionError BhashiniUpstreamError
            UnsupportedLanguageError: mapped by the caller.
        """
        return self._bhashini.synthesize(text, language)

    # ---- local dev upload -------------------------------------------------

    def verify_local_upload(self, key: str, expires: int, signature: str) -> bool:
        """Whether this local-provider PUT carries a signature we issued.

        The route this backs is deliberately unauthenticated -- a presigned URL
        is used by a bare `fetch` with no Authorization header -- so this HMAC
        check is the ONLY thing standing between it and a
        write-anything-anywhere endpoint.
        """
        return bool(self._store.verify(key, expires, signature))

    def store_local_upload(self, key: str, body: bytes) -> None:
        """:raises OSError StorageError: mapped by the caller."""
        self._store.put(key, body)

    def is_local_provider(self) -> bool:
        return getattr(self._store, "provider", None) == "local"


def language_for(requested: Any, user: UserContext) -> Optional[str]:
    """The requested voice language, defaulting to the caller's locale.

    Voice language is a UI preference and does NOT change the conversation:
    `UserContext.locale` is the fallback, not the authority, and nothing here
    writes back to it. Full multilingual chat would mean threading a language
    through /api/chat, which is separate work.

    Returns None for an unsupported language, which the caller maps to 400.
    """
    raw = requested or user.locale or "en"
    language = str(raw).strip().lower()
    return language if language in SUPPORTED_LANGUAGES else None
