"""Speech-to-text and text-to-speech.

THREE ENDPOINTS, AND THE CHAT PIPELINE IS UNTOUCHED. Transcription produces text
that the user reviews in the composer and then sends through the normal
`/api/chat`; synthesis consumes text that pipeline already produced. Nothing
here reaches `OrchestrationService`, so `/api/chat`'s pinned contract and its
characterisation fixtures are unaffected.

THE UPLOAD FLOW, and why it has three steps:

    POST /api/voice/upload-url   -> {uploadUrl, objectKey}
    PUT  <uploadUrl>             -> the browser sends bytes straight to the bucket
    POST /api/voice/transcribe   -> {objectKey} -> transcript

Audio does not pass through this API. Ten seconds of Opus is small, but a worker
thread here also holds a database connection for the whole request
(THREADPOOL_SIZE <= DB_POOL_SIZE), and uploads are the slowest and least
predictable part of the cycle -- a user on hotel wifi would hold one for the
duration. Mitra's frontend uploads to S3 the same way for the same reason.

OBJECTS ARE ADDRESSED BY KEY, NOT BY URL. Mitra's `/api/asr/` takes `{s3Url}`
and fetches whatever it is given, which needs an allowlist check to not be an
SSRF primitive. A key names an object inside a bucket fixed by configuration, so
there is no attacker-controlled destination at all. `_owned_key` still checks
that the key belongs to one of the caller's own conversations -- otherwise the
key would be a read primitive over other users' recordings.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.body import json_body_strict, raw_body
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.core import UserContext
from app.exceptions.envelope import error_response
from app.integrations.bhashini import (
    AudioConversionError,
    BhashiniTimeout,
    BhashiniUpstreamError,
    SUPPORTED_LANGUAGES,
    UnsupportedLanguageError,
)
from app.integrations.storage import (
    StorageDownloadError,
    StorageError,
    StorageTooLargeError,
)
from app.repositories.conversations import ConversationRepository
from app.utils.responses import json_response, parse_uuid

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

KEY_PREFIX = "voice"

# What the browser may record in. Extensions are for the object key only --
# ffmpeg detects the container from content, so a wrong guess is cosmetic.
CONTENT_TYPE_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}
DEFAULT_EXTENSION = "webm"


def _voice_parts(container: Container):
    """The Bhashini client and object store, or None when voice is disabled.

    Both or neither -- `build_container` builds them together, so one being
    present and the other missing is not a state this can observe.
    """
    return container.bhashini, container.object_store


def _language(data: Dict[str, Any], user: UserContext) -> Optional[str]:
    """The requested voice language, defaulting to the caller's locale.

    Voice language is a UI preference and does NOT change the conversation:
    `UserContext.locale` is the fallback, not the authority, and nothing here
    writes back to it. Full multilingual chat would mean threading a language
    through /api/chat, which is separate work.
    """
    raw = data.get("language") or user.locale or "en"
    language = str(raw).strip().lower()
    return language if language in SUPPORTED_LANGUAGES else None


def _owned_key(raw: Any, user: UserContext, db: Session) -> Optional[str]:
    """Validate an object key and confirm the caller owns its conversation.

    Keys are `voice/{conversation_id}/{uuid}.{ext}` and server-generated, so
    the shape check is cheap. The ownership check is the one that matters: a
    key is otherwise a read primitive over every recording in the bucket.

    Returns None for anything that fails, and the caller answers 404 for all of
    them -- distinguishing "malformed", "not yours" and "no such conversation"
    would confirm which conversation ids exist.
    """
    if not isinstance(raw, str):
        return None
    key = raw.strip()
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return None
    # No traversal, no absolute paths: every segment must be a plain name. The
    # local driver checks containment again next to the filesystem call, but a
    # caller should never get that far.
    if any(part in ("", ".", "..") or "\\" in part for part in parts):
        return None

    conversation_id = parse_uuid(parts[1])
    if conversation_id is None:
        return None
    if ConversationRepository(db).get_scoped(conversation_id, user) is None:
        return None
    return key


@router.post("/api/voice/upload-url", response_model=None)
def upload_url(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Presign a direct-to-bucket upload for one recording.

    Answers with what the browser should DO rather than just where to send it
    -- method and headers included -- so one frontend code path drives an S3
    presigned PUT, a GCS V4 signed PUT and the local dev route unchanged.
    """
    _, store = _voice_parts(container)
    if store is None:
        return error_response("Voice is not enabled.", "VOICE_DISABLED", 503)

    data = data or {}
    conversation_id = parse_uuid(data.get("conversation_id"))
    if conversation_id is None:
        return error_response("conversation_id is required", "INVALID_REQUEST", 400)
    if ConversationRepository(db).get_scoped(conversation_id, user) is None:
        return error_response("Conversation not found", "NOT_FOUND", 404)

    content_type = str(data.get("content_type") or "audio/webm").split(";")[0].strip().lower()
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type, DEFAULT_EXTENSION)

    # SERVER-GENERATED. A client-supplied key is an arbitrary-write primitive,
    # and embedding the conversation id is what makes the ownership check on
    # the way back in possible.
    key = f"{KEY_PREFIX}/{conversation_id}/{uuid.uuid4().hex}.{extension}"

    try:
        presigned = store.presign_put(key, content_type, expires_s=300)
    except StorageError as exc:
        logger.warning("voice: presign failed (%s)", type(exc).__name__)
        return error_response("Could not prepare the upload.", "STORAGE_ERROR", 502)

    return json_response({
        "uploadUrl": presigned.url,
        "method": presigned.method,
        "headers": presigned.headers,
        "objectKey": presigned.key,
        "expiresIn": presigned.expires_in_s,
    })


@router.post("/api/voice/transcribe", response_model=None)
def transcribe(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Transcribe an uploaded recording.

    The transcript goes back to the composer for the user to review, never
    straight into a turn -- ASR on a t4-class model over code-mixed speech is
    good, not right, and a visible edit box is the mitigation.
    """
    bhashini, store = _voice_parts(container)
    if bhashini is None or store is None:
        return error_response("Voice is not enabled.", "VOICE_DISABLED", 503)

    data = data or {}
    key = _owned_key(data.get("objectKey"), user, db)
    if key is None:
        return error_response("Recording not found", "NOT_FOUND", 404)

    language = _language(data, user)
    if language is None:
        return error_response(
            f"language must be one of: {', '.join(SUPPORTED_LANGUAGES)}",
            "INVALID_REQUEST",
            400,
        )

    settings = container.settings
    try:
        audio = store.fetch(key, max_bytes=settings.voice_max_audio_bytes)
    except StorageTooLargeError:
        return error_response("That recording is too long.", "AUDIO_TOO_LARGE", 413)
    except StorageDownloadError:
        return error_response("Recording not found", "NOT_FOUND", 404)
    except StorageError as exc:
        logger.warning("voice: fetch failed (%s)", type(exc).__name__)
        return error_response("Could not read the recording.", "STORAGE_ERROR", 502)

    try:
        transcript = bhashini.transcribe(audio, language)
    except BhashiniTimeout as exc:
        return error_response(str(exc), "UPSTREAM_TIMEOUT", 504)
    except AudioConversionError as exc:
        logger.warning("voice: conversion failed (%s)", exc)
        return error_response("That recording could not be read.", "ASR_FAILED", 502)
    except (BhashiniUpstreamError, UnsupportedLanguageError) as exc:
        logger.warning("voice: asr failed (%s)", exc)
        return error_response("Could not transcribe that recording.", "ASR_FAILED", 502)
    finally:
        # Best effort, and outside the success path on purpose: the recording is
        # PII and has no further use once transcribed. The bucket lifecycle rule
        # is the actual guarantee, because this does not run when the process
        # dies mid-request.
        store.delete(key)

    # The LLM works in English, so a Hindi recording is translated before it
    # reaches the composer -- which is what keeps /api/chat monolingual and its
    # contract unchanged. A failed translation returns the Indic transcript
    # rather than nothing: the user can see it, and losing their words to a
    # translation outage would be worse than showing them untranslated.
    if transcript and language != "en":
        try:
            transcript = bhashini.translate(transcript, language, "en")
        except (BhashiniUpstreamError, BhashiniTimeout) as exc:
            logger.warning("voice: translation failed, returning source text (%s)", exc)

    return json_response({"transcript": transcript, "language": language})


@router.post("/api/voice/speak", response_model=None)
def speak(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
) -> JSONResponse:
    """Synthesise speech for one agent reply.

    Takes text rather than a message id so it stays independent of the
    transcript store -- the frontend already holds the reply, and reading it
    back from the database would only add a lookup that can disagree.
    """
    bhashini, _ = _voice_parts(container)
    if bhashini is None:
        return error_response("Voice is not enabled.", "VOICE_DISABLED", 503)

    data = data or {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return error_response("text is required", "INVALID_REQUEST", 400)

    language = _language(data, user)
    if language is None:
        return error_response(
            f"language must be one of: {', '.join(SUPPORTED_LANGUAGES)}",
            "INVALID_REQUEST",
            400,
        )

    try:
        result = bhashini.synthesize(text, language)
    except BhashiniTimeout as exc:
        return error_response(str(exc), "UPSTREAM_TIMEOUT", 504)
    except (AudioConversionError, BhashiniUpstreamError, UnsupportedLanguageError) as exc:
        logger.warning("voice: tts failed (%s)", exc)
        return error_response("Could not read that out.", "TTS_FAILED", 502)

    return json_response({"audio": result["audio"], "format": result["format"]})


@router.put("/api/voice/upload-local/{key:path}", response_model=None)
def upload_local(
    key: str,
    request: Request,
    body: bytes = Depends(raw_body),
    container: Container = Depends(get_container),
) -> Response:
    """Receive an upload for the `local` storage provider. DEV ONLY.

    There is no storage origin to PUT to when files live on disk, so the local
    driver presigns back to this route.

    DELIBERATELY UNAUTHENTICATED, and safe only because of the signature. A
    presigned URL is used by a bare `fetch` with no Authorization header, so
    `get_current_user` cannot apply -- the HMAC the local driver issued is what
    stands in for it, and without that check this would be a
    write-anything-anywhere endpoint. It 404s under every other provider, so it
    does not exist at all in a deployment.
    """
    store = container.object_store
    if store is None or getattr(store, "provider", None) != "local":
        return error_response("Not found", "NOT_FOUND", 404)

    try:
        expires = int(request.query_params.get("expires", "0"))
    except (TypeError, ValueError):
        return error_response("Invalid upload URL", "INVALID_REQUEST", 400)

    signature = request.query_params.get("signature", "")
    if not store.verify(key, expires, signature):
        return error_response("Invalid or expired upload URL", "INVALID_REQUEST", 403)

    if not body:
        return error_response("Empty upload", "INVALID_REQUEST", 400)

    try:
        store.put(key, body)
    except (OSError, StorageError) as exc:
        logger.warning("voice: local upload failed (%s)", type(exc).__name__)
        return error_response("Could not store the recording.", "STORAGE_ERROR", 502)

    return Response(status_code=204)
