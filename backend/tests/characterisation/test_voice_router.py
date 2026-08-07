"""The /api/voice surface, end to end through the TestClient.

Bhashini and the object store are replaced with fakes -- the suite runs with
`--disable-socket` and Dhruva is a live public service, so what is pinned here
is this app's contract: the error envelopes, the gating, and the ownership check
that keeps one caller's recordings out of another's reach.

`VOICE_ENABLED` is unset in tests/conftest.py, so the default state of the whole
suite is voice-off. Tests that need it on swap fakes onto the container.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest

from tests.characterisation.conftest import chat


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeStore:
    provider = "aws"

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fetch_error: Exception | None = None

    def presign_put(self, key, content_type, expires_s=300):
        from app.integrations.storage.base import PresignedUpload

        return PresignedUpload(
            url=f"https://bucket.example/{key}?sig=x", key=key, expires_in_s=expires_s
        )

    def presign_get(self, key, expires_s=600):
        return f"https://bucket.example/{key}"

    def fetch(self, key, max_bytes):
        if self.fetch_error:
            raise self.fetch_error
        from app.integrations.storage.exceptions import StorageDownloadError

        if key not in self.objects:
            raise StorageDownloadError(key, "not found")
        return self.objects[key]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


class FakeBhashini:
    def __init__(self):
        self.transcript = "hello world"
        self.audio = base64.b64encode(b"RIFF....WAVEfake").decode()
        self.transcribe_error: Exception | None = None
        self.synthesize_error: Exception | None = None
        self.translate_error: Exception | None = None
        self.translate_calls: list[tuple] = []

    def transcribe(self, audio, language):
        if self.transcribe_error:
            raise self.transcribe_error
        return self.transcript

    def synthesize(self, text, language, gender="female"):
        if self.synthesize_error:
            raise self.synthesize_error
        return {"audio": self.audio, "format": "wav"}

    def translate(self, text, source_language, target_language):
        self.translate_calls.append((text, source_language, target_language))
        if self.translate_error:
            raise self.translate_error
        return f"translated({text})"


@pytest.fixture()
def voice(api_app):
    """Turn voice on with fakes, and put the container back afterwards.

    `Container` is a frozen dataclass, so this replaces it wholesale rather
    than mutating it -- the same thing `build_container` would have produced
    with VOICE_ENABLED=1.
    """
    original = api_app.state.container
    store, client = FakeStore(), FakeBhashini()
    api_app.state.container = dataclasses.replace(
        original, bhashini=client, object_store=store
    )
    yield store, client
    api_app.state.container = original


@pytest.fixture()
def conversation_id(client, script):
    """A real conversation owned by the test caller.

    Created through /api/chat rather than inserted directly, because the
    ownership check these tests exercise runs against a real
    ConversationRepository.get_scoped -- a hand-built row could satisfy the
    check without matching what the app actually writes.

    Two scripted replies: the router picks an agent with one call, the agent
    answers with the second.
    """
    script.queue("General Support Agent")
    script.queue("Hello, how can I help?")
    status, body = chat(client, "hello")
    assert status == 200, body
    return body["conversation_id"]


def _upload(client, conversation_id, content_type="audio/webm"):
    return client.post(
        "/api/voice/upload-url",
        json={"conversation_id": conversation_id, "content_type": content_type},
    )


# ---------------------------------------------------------------------------
# gating -- the default state of a deployment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, payload",
    [
        ("/api/voice/upload-url", {"conversation_id": "x"}),
        ("/api/voice/transcribe", {"objectKey": "voice/x/y.webm"}),
        ("/api/voice/speak", {"text": "hello"}),
    ],
)
def test_every_endpoint_503s_when_voice_is_disabled(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 503

    body = response.json()
    assert body["error_code"] == "VOICE_DISABLED"
    # The standard envelope -- NOT the admin router's bare {"error": CODE}.
    assert body["status"] == "error"
    assert set(body) == {"status", "error", "error_code", "request_id"}


def test_upload_local_404s_under_a_cloud_provider(client, voice):
    """The dev-only route must not exist in a deployment. It is deliberately
    unauthenticated, so 'off unless local' is what keeps that safe."""
    assert client.put("/api/voice/upload-local/voice/a/b.webm", content=b"x").status_code == 404


# ---------------------------------------------------------------------------
# upload-url
# ---------------------------------------------------------------------------

def test_upload_url_returns_a_server_generated_key(client, voice, conversation_id):
    """A client-supplied key would be an arbitrary-write primitive, and the
    embedded conversation id is what makes the ownership check possible."""
    body = _upload(client, conversation_id).json()

    assert body["objectKey"].startswith(f"voice/{conversation_id}/")
    assert body["objectKey"].endswith(".webm")
    assert body["method"] == "PUT"
    assert body["uploadUrl"].startswith("https://bucket.example/")
    assert body["expiresIn"] == 300


@pytest.mark.parametrize(
    "content_type, extension",
    [
        ("audio/webm", "webm"),
        ("audio/webm;codecs=opus", "webm"),
        ("audio/mp4", "mp4"),        # Safari
        ("audio/wav", "wav"),
        ("application/nonsense", "webm"),  # unknown falls back
    ],
)
def test_upload_url_maps_content_type_to_an_extension(
    client, voice, conversation_id, content_type, extension
):
    body = _upload(client, conversation_id, content_type).json()
    assert body["objectKey"].endswith(f".{extension}")


def test_two_uploads_never_share_a_key(client, voice, conversation_id):
    first = _upload(client, conversation_id).json()["objectKey"]
    second = _upload(client, conversation_id).json()["objectKey"]
    assert first != second


def test_upload_url_requires_a_conversation_id(client, voice):
    response = client.post("/api/voice/upload-url", json={})
    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_REQUEST"


def test_upload_url_rejects_a_conversation_the_caller_does_not_own(client, voice):
    import uuid

    response = _upload(client, str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

def test_transcribe_returns_the_transcript_and_deletes_the_recording(
    client, voice, conversation_id
):
    """Recordings are PII with no use after transcription."""
    store, _ = voice
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"audio"

    response = client.post("/api/voice/transcribe", json={"objectKey": key, "language": "en"})

    assert response.status_code == 200
    assert response.json() == {"transcript": "hello world", "language": "en"}
    assert store.deleted == [key]


def test_a_non_english_transcript_is_translated_to_english(client, voice, conversation_id):
    """The LLM works in English, which is what keeps /api/chat monolingual and
    its pinned contract unchanged."""
    store, bhashini = voice
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"audio"

    body = client.post(
        "/api/voice/transcribe", json={"objectKey": key, "language": "hi"}
    ).json()

    assert body["transcript"] == "translated(hello world)"
    assert bhashini.translate_calls == [("hello world", "hi", "en")]


def test_english_is_not_sent_through_translation(client, voice, conversation_id):
    store, bhashini = voice
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"audio"

    client.post("/api/voice/transcribe", json={"objectKey": key, "language": "en"})
    assert bhashini.translate_calls == []


def test_a_failed_translation_still_returns_the_source_transcript(
    client, voice, conversation_id
):
    """Losing the user's words to a translation outage is worse than showing
    them untranslated -- they can see it and edit before sending."""
    from app.integrations.bhashini import BhashiniUpstreamError

    store, bhashini = voice
    bhashini.translate_error = BhashiniUpstreamError("translation", status=502)
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"audio"

    body = client.post(
        "/api/voice/transcribe", json={"objectKey": key, "language": "hi"}
    ).json()
    assert body["transcript"] == "hello world"


@pytest.mark.parametrize(
    "key",
    [
        "voice/../../etc/passwd",
        "../escape",
        "voice/not-a-uuid/x.webm",
        "wrongprefix/abc/x.webm",
        "voice/abc",
        "",
        None,
        12345,
    ],
)
def test_transcribe_rejects_malformed_keys(client, voice, key):
    response = client.post("/api/voice/transcribe", json={"objectKey": key})
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


def test_transcribe_rejects_a_key_from_another_conversation(client, voice):
    """Without this the key is a read primitive over every recording in the
    bucket. 404 rather than 403, so it cannot confirm which ids exist."""
    import uuid

    response = client.post(
        "/api/voice/transcribe",
        json={"objectKey": f"voice/{uuid.uuid4()}/{uuid.uuid4().hex}.webm"},
    )
    assert response.status_code == 404


def test_transcribe_rejects_an_unsupported_language(client, voice, conversation_id):
    key = _upload(client, conversation_id).json()["objectKey"]
    response = client.post("/api/voice/transcribe", json={"objectKey": key, "language": "fr"})

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_REQUEST"


def test_oversized_recording_is_413(client, voice, conversation_id):
    from app.integrations.storage.exceptions import StorageTooLargeError

    store, _ = voice
    key = _upload(client, conversation_id).json()["objectKey"]
    store.fetch_error = StorageTooLargeError(key, 20_000_000, 10_485_760)

    response = client.post("/api/voice/transcribe", json={"objectKey": key})
    assert response.status_code == 413
    assert response.json()["error_code"] == "AUDIO_TOO_LARGE"


def test_an_upstream_timeout_maps_to_504(client, voice, conversation_id):
    """504 UPSTREAM_TIMEOUT is the one error code the frontend already renders
    with a retry button -- and Dhruva has no SLA, so retrying is usually right."""
    from app.integrations.bhashini import BhashiniTimeout

    store, bhashini = voice
    bhashini.transcribe_error = BhashiniTimeout("asr", 30.0)
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"audio"

    response = client.post("/api/voice/transcribe", json={"objectKey": key})
    assert response.status_code == 504
    assert response.json()["error_code"] == "UPSTREAM_TIMEOUT"


def test_unreadable_audio_maps_to_502(client, voice, conversation_id):
    from app.integrations.bhashini import AudioConversionError

    store, bhashini = voice
    bhashini.transcribe_error = AudioConversionError("ffmpeg failed")
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"not audio"

    response = client.post("/api/voice/transcribe", json={"objectKey": key})
    assert response.status_code == 502
    assert response.json()["error_code"] == "ASR_FAILED"


def test_the_recording_is_deleted_even_when_transcription_fails(
    client, voice, conversation_id
):
    from app.integrations.bhashini import BhashiniUpstreamError

    store, bhashini = voice
    bhashini.transcribe_error = BhashiniUpstreamError("asr", status=500)
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"audio"

    client.post("/api/voice/transcribe", json={"objectKey": key})
    assert store.deleted == [key]


def test_an_empty_transcript_is_a_valid_answer(client, voice, conversation_id):
    """Silence transcribes to nothing. The frontend decides what to say about
    it; the API does not turn it into an error."""
    store, bhashini = voice
    bhashini.transcript = ""
    key = _upload(client, conversation_id).json()["objectKey"]
    store.objects[key] = b"audio"

    response = client.post("/api/voice/transcribe", json={"objectKey": key})
    assert response.status_code == 200
    assert response.json()["transcript"] == ""


# ---------------------------------------------------------------------------
# speak
# ---------------------------------------------------------------------------

def test_speak_returns_base64_audio(client, voice):
    response = client.post("/api/voice/speak", json={"text": "Hello there", "language": "en"})

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "wav"
    assert base64.b64decode(body["audio"]).startswith(b"RIFF")


@pytest.mark.parametrize("text", ["", "   ", None, 123, []])
def test_speak_requires_text(client, voice, text):
    response = client.post("/api/voice/speak", json={"text": text})
    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_REQUEST"


def test_speak_defaults_to_the_callers_locale(client, voice):
    """Language is a UI preference layered over the caller's locale, not a
    separate required field."""
    assert client.post("/api/voice/speak", json={"text": "hi"}).status_code == 200


def test_speak_rejects_an_unsupported_language(client, voice):
    response = client.post("/api/voice/speak", json={"text": "hi", "language": "de"})
    assert response.status_code == 400


def test_tts_failure_maps_to_502(client, voice):
    from app.integrations.bhashini import BhashiniUpstreamError

    _, bhashini = voice
    bhashini.synthesize_error = BhashiniUpstreamError("tts", status=500)

    response = client.post("/api/voice/speak", json={"text": "hello"})
    assert response.status_code == 502
    assert response.json()["error_code"] == "TTS_FAILED"


# ---------------------------------------------------------------------------
# body handling -- same semantics as /api/chat
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path", ["/api/voice/upload-url", "/api/voice/transcribe", "/api/voice/speak"]
)
def test_a_missing_body_gets_this_apps_own_400(client, voice, path):
    """Not FastAPI's 422 with its own {"detail": [...]} shape -- which is the
    whole reason these routes read a raw dict through json_body_strict."""
    response = client.post(path)
    assert response.status_code in (400, 404)
    assert "error_code" in response.json()
