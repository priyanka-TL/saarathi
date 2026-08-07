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
there is no attacker-controlled destination at all. `VoiceService.owned_key`
still checks that the key belongs to one of the caller's own conversations --
otherwise the key would be a read primitive over other users' recordings.

WHAT THIS MODULE DOES AND DOES NOT DO. The workflow -- fetch, transcribe, delete,
translate -- lives in `app/services/voice_service.py`. What stays here is the
mapping from a raised integration exception to a status code and an envelope,
which is an HTTP decision and belongs in the API layer. Every route below is
therefore a validate / delegate / map-errors sandwich.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.dependencies.body import json_body_strict, raw_body
from app.dependencies.identity import get_current_user
from app.dependencies.voice import get_voice_service
from app.domain.core import UserContext
from app.exceptions.envelope import error_response
from app.integrations.bhashini import (
    AudioConversionError,
    BhashiniTimeout,
    BhashiniUpstreamError,
    SUPPORTED_LANGUAGES,
    UnsupportedLanguageError,
)
from app.integrations.bhashini.media_types import DEFAULT_CONTENT_TYPE
from app.integrations.storage import (
    StorageDownloadError,
    StorageError,
    StorageTooLargeError,
)
from app.services.voice_service import VoiceService, language_for
from app.utils.responses import json_response, parse_uuid

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


def _disabled() -> JSONResponse:
    return error_response("Voice is not enabled.", "VOICE_DISABLED", 503)


def _bad_language() -> JSONResponse:
    return error_response(
        f"language must be one of: {', '.join(SUPPORTED_LANGUAGES)}",
        "INVALID_REQUEST",
        400,
    )


@router.post("/api/voice/upload-url", response_model=None)
def upload_url(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    voice: VoiceService = Depends(get_voice_service),
    user: UserContext = Depends(get_current_user),
) -> JSONResponse:
    """Presign a direct-to-bucket upload for one recording.

    Answers with what the browser should DO rather than just where to send it
    -- method and headers included -- so one frontend code path drives an S3
    presigned PUT, a GCS V4 signed PUT and the local dev route unchanged.
    """
    if not voice.enabled:
        return _disabled()

    data = data or {}
    conversation_id = parse_uuid(data.get("conversation_id"))
    if conversation_id is None:
        return error_response("conversation_id is required", "INVALID_REQUEST", 400)
    if not voice.owns_conversation(conversation_id, user):
        return error_response("Conversation not found", "NOT_FOUND", 404)

    content_type = str(
        data.get("content_type") or DEFAULT_CONTENT_TYPE
    ).split(";")[0].strip().lower()

    try:
        presigned = voice.presign_upload(conversation_id, content_type)
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
    voice: VoiceService = Depends(get_voice_service),
    user: UserContext = Depends(get_current_user),
) -> JSONResponse:
    """Transcribe an uploaded recording.

    The transcript goes back to the composer for the user to review, never
    straight into a turn -- ASR on a t4-class model over code-mixed speech is
    good, not right, and a visible edit box is the mitigation.
    """
    if not voice.enabled:
        return _disabled()

    data = data or {}
    # 404 for a malformed key, a key that is not the caller's, and a
    # conversation that does not exist alike -- see VoiceService.owned_key.
    key = voice.owned_key(data.get("objectKey"), user)
    if key is None:
        return error_response("Recording not found", "NOT_FOUND", 404)

    language = language_for(data.get("language"), user)
    if language is None:
        return _bad_language()

    try:
        transcript = voice.transcribe(key, language)
    except StorageTooLargeError:
        return error_response("That recording is too long.", "AUDIO_TOO_LARGE", 413)
    except StorageDownloadError:
        return error_response("Recording not found", "NOT_FOUND", 404)
    except StorageError as exc:
        logger.warning("voice: fetch failed (%s)", type(exc).__name__)
        return error_response("Could not read the recording.", "STORAGE_ERROR", 502)
    except BhashiniTimeout as exc:
        return error_response(str(exc), "UPSTREAM_TIMEOUT", 504)
    except AudioConversionError as exc:
        logger.warning("voice: conversion failed (%s)", exc)
        return error_response("That recording could not be read.", "ASR_FAILED", 502)
    except (BhashiniUpstreamError, UnsupportedLanguageError) as exc:
        logger.warning("voice: asr failed (%s)", exc)
        return error_response("Could not transcribe that recording.", "ASR_FAILED", 502)

    return json_response({"transcript": transcript, "language": language})


@router.post("/api/voice/speak", response_model=None)
def speak(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    voice: VoiceService = Depends(get_voice_service),
    user: UserContext = Depends(get_current_user),
) -> JSONResponse:
    """Synthesise speech for one agent reply.

    Takes text rather than a message id so it stays independent of the
    transcript store -- the frontend already holds the reply, and reading it
    back from the database would only add a lookup that can disagree.
    """
    # NOT `voice.enabled`: synthesis never touches the object store, and this
    # route has always gated on the speech client alone. Widening it would let a
    # storage misconfiguration silently take TTS down with it.
    if not voice.speech_enabled:
        return _disabled()

    data = data or {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return error_response("text is required", "INVALID_REQUEST", 400)

    language = language_for(data.get("language"), user)
    if language is None:
        return _bad_language()

    try:
        result = voice.synthesize(text, language)
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
    voice: VoiceService = Depends(get_voice_service),
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
    if not voice.enabled or not voice.is_local_provider():
        return error_response("Not found", "NOT_FOUND", 404)

    try:
        expires = int(request.query_params.get("expires", "0"))
    except (TypeError, ValueError):
        return error_response("Invalid upload URL", "INVALID_REQUEST", 400)

    signature = request.query_params.get("signature", "")
    if not voice.verify_local_upload(key, expires, signature):
        return error_response("Invalid or expired upload URL", "INVALID_REQUEST", 403)

    if not body:
        return error_response("Empty upload", "INVALID_REQUEST", 400)

    try:
        voice.store_local_upload(key, body)
    except (OSError, StorageError) as exc:
        logger.warning("voice: local upload failed (%s)", type(exc).__name__)
        return error_response("Could not store the recording.", "STORAGE_ERROR", 502)

    return Response(status_code=204)
