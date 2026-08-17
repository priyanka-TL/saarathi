"""The Bhashini integration: markdown stripping, chunking, and the HTTP client.

No network. `responses` intercepts the transport, following
`tests/integration/mitra/test_mitra_rest_client.py`.

The `strip_markdown_for_tts` cases are not decoration. Four of its regexes only
work in the position they occupy, and the failure mode is subtle -- a stray "!",
a lost first word -- so each ordering constraint gets a test that fails if the
rules are ever regrouped.
"""

from __future__ import annotations

import base64
import io
import wave

import pytest
import responses

from app.integrations.bhashini.audio import (
    detect_audio_format,
    merge_audio_base64,
    split_audio,
)
from app.integrations.bhashini.client import BhashiniClient
from app.integrations.bhashini.exceptions import (
    AudioConversionError,
    BhashiniTimeout,
    BhashiniUpstreamError,
    UnsupportedLanguageError,
)
from app.integrations.bhashini.text import (
    split_text_for_tts,
    strip_markdown_for_tts,
)

BASE_URL = "https://dhruva.example/services/inference/pipeline"


def _client(**overrides) -> BhashiniClient:
    # Every tuning knob is stated explicitly: BhashiniClient takes no defaults,
    # so that Settings stays the single declaration of each production value.
    kwargs = {
        "base_url": BASE_URL,
        "authorization": "inference-key",
        "api_key": "ulca-key",
        "user_id": "ulca-user",
        "connect_timeout": 10.0,
        "read_timeout": 30.0,
        "chunk_duration_s": 10,
        "tts_byte_limit": 4800,
        "asr_max_workers": 4,
        "ffmpeg_timeout_s": 30.0,
    }
    kwargs.update(overrides)
    return BhashiniClient(**kwargs)


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# strip_markdown_for_tts -- the ordering constraints
# ---------------------------------------------------------------------------

def test_images_are_stripped_before_links():
    """Images must precede links, or `![alt](url)` is consumed by the link rule
    and leaves a stray '!' for the synthesiser to read out."""
    assert strip_markdown_for_tts("![a diagram](x.png)") == "a diagram"
    assert "!" not in strip_markdown_for_tts("See ![chart](c.png) here")


def test_br_becomes_a_period_before_tags_are_stripped():
    """<br> must become '. ' before the general tag strip, or the pause is lost.
    The LLM emits <br> inside table cells constantly."""
    assert strip_markdown_for_tts("one<br>two") == "one. two"
    assert strip_markdown_for_tts("one<br/>two") == "one. two"
    assert strip_markdown_for_tts("one<BR />two") == "one. two"


def test_doubled_periods_from_br_are_cleaned_up():
    """Text already ending in punctuation reads '..' after the <br> rule."""
    assert strip_markdown_for_tts("Done.<br>Next") == "Done. Next"


def test_bullets_are_stripped_before_bold():
    """Bullet markers must precede bold/italic, or '* **bold**' is read as one
    nested run of asterisks and the line loses its first word."""
    assert strip_markdown_for_tts("* **Ravi** leads") == "Ravi leads"
    assert strip_markdown_for_tts("- **Priya** reports") == "Priya reports"


def test_times_do_not_get_read_as_ratios():
    assert strip_markdown_for_tts("Meet at 1:30") == "Meet at 1, 30"
    # A colon that is not between digits is left alone.
    assert strip_markdown_for_tts("Note: this") == "Note: this"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("```python\nprint(1)\n```", ""),
        # The gap left by the removed code span is closed by the later
        # multi-space collapse, so this reads as one clean sentence.
        ("use `x = 1` here", "use here"),
        ("[Saarthi](https://s.example)", "Saarthi"),
        ("# Heading", "Heading"),
        ("> quoted", "quoted"),
        ("**bold** and _italic_", "bold and italic"),
        ("• a bullet", "a bullet"),
        ("a | b | c", "a b c"),
        ("---", ""),
        ("<p>html</p>", "html"),
    ],
)
def test_markdown_constructs_are_removed(raw, expected):
    assert strip_markdown_for_tts(raw) == expected


def test_none_and_empty_are_safe():
    """Every agent reply hits this, including the degenerate ones."""
    assert strip_markdown_for_tts(None) == ""
    assert strip_markdown_for_tts("") == ""


def test_a_markdown_table_survives_as_speakable_text():
    spoken = strip_markdown_for_tts(
        "| Name | Role |\n|------|------|\n| Ravi | Lead |"
    )
    assert "|" not in spoken and "---" not in spoken
    assert "Ravi" in spoken and "Lead" in spoken


# ---------------------------------------------------------------------------
# split_text_for_tts
# ---------------------------------------------------------------------------

def test_text_under_the_limit_is_one_chunk():
    assert split_text_for_tts("short", 4800) == ["short"]


def test_devanagari_danda_is_a_sentence_boundary():
    """Without U+0964 Hindi has no sentence boundaries at all and every reply
    falls through to the word splitter."""
    text = "पहला वाक्य। " * 100
    chunks = split_text_for_tts(text, 200)
    assert len(chunks) > 1
    assert all(len(c.encode("utf-8")) <= 200 for c in chunks)


def test_the_limit_is_bytes_not_characters():
    """One Devanagari character is three UTF-8 bytes, so a limit that looks
    generous in characters is not."""
    chunks = split_text_for_tts("नमस्ते दुनिया। " * 50, 300)
    assert all(len(c.encode("utf-8")) <= 300 for c in chunks)


def test_an_oversized_single_word_is_split_by_character():
    chunks = split_text_for_tts("x" * 500, 100)
    assert len(chunks) > 1
    assert all(len(c.encode("utf-8")) <= 100 for c in chunks)


def test_splitting_preserves_every_word():
    text = " ".join(f"word{i}." for i in range(400))
    assert " ".join(split_text_for_tts(text, 200)).split() == text.split()


# ---------------------------------------------------------------------------
# audio helpers
# ---------------------------------------------------------------------------

def test_detect_audio_format_reads_the_riff_magic():
    assert detect_audio_format(b"RIFF....WAVE") == "wav"
    assert detect_audio_format(b"\xff\xfb\x90\x00") == "mp3"


def test_split_audio_produces_self_contained_wavs():
    """Each chunk carries its own RIFF header, so it can be base64-encoded and
    posted to Bhashini directly."""
    chunks = split_audio(_wav(seconds=25), chunk_duration_s=10)
    assert [i for i, _ in chunks] == [0, 1, 2]
    for _, data in chunks:
        with wave.open(io.BytesIO(data), "rb") as w:
            assert w.getframerate() == 16000
            assert w.getnchannels() == 1


def test_split_audio_rejects_non_wav():
    with pytest.raises(AudioConversionError):
        split_audio(b"this is not a wav file", chunk_duration_s=10)


def test_merge_returns_a_single_chunk_untouched():
    only = base64.b64encode(_wav(0.5)).decode()
    assert merge_audio_base64([only], "wav") is only


def test_merged_wav_has_the_summed_duration_and_one_header():
    """Raw concatenation would embed a RIFF header mid-stream and every player
    stops at the first one, so WAV must go through the wave writer."""
    parts = [base64.b64encode(_wav(1.0)).decode() for _ in range(3)]
    merged = base64.b64decode(merge_audio_base64(parts, "wav"))

    with wave.open(io.BytesIO(merged), "rb") as w:
        assert w.getnframes() == 3 * 16000
        assert w.getframerate() == 16000
    assert merged.count(b"RIFF") == 1


def test_mp3_chunks_are_concatenated_raw():
    parts = [base64.b64encode(b"\xff\xfbframe").decode() for _ in range(3)]
    assert base64.b64decode(merge_audio_base64(parts, "mp3")) == b"\xff\xfbframe" * 3


# ---------------------------------------------------------------------------
# client -- ASR
# ---------------------------------------------------------------------------

def _asr_body(text: str) -> dict:
    return {"pipelineResponse": [{"output": [{"source": text}]}]}


@responses.activate
def test_asr_sends_all_three_auth_headers():
    """Asymmetric on purpose, and faithful to Mitra: ASR sends Authorization +
    userID + ulcaApiKey, TTS and translation send Authorization alone."""
    responses.post(BASE_URL, json=_asr_body("hello"))

    assert _client()._transcribe_chunk(_wav(1.0), "en") == "hello"

    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == "inference-key"
    assert headers["userID"] == "ulca-user"
    assert headers["ulcaApiKey"] == "ulca-key"


@responses.activate
def test_asr_payload_shape():
    responses.post(BASE_URL, json=_asr_body("x"))
    _client()._transcribe_chunk(_wav(1.0), "hi")

    import json

    task = json.loads(responses.calls[0].request.body)["pipelineTasks"][0]
    assert task["taskType"] == "asr"
    assert task["config"]["language"] == {"sourceLanguage": "hi"}
    assert task["config"]["samplingRate"] == 16000
    assert task["config"]["audioFormat"] == "wav"
    # Hindi gets the Hindi conformer, NOT the Dravidian model Mitra defaults
    # every language to.
    assert task["config"]["serviceId"] == "ai4bharat/conformer-hi-gpu--t4"


@responses.activate
def test_asr_reassembles_chunks_in_index_order_not_completion_order():
    """The whole reason chunks are numbered. Responses are returned in a
    deliberately shuffled order relative to the requests."""
    for word in ["one", "two", "three"]:
        responses.post(BASE_URL, json=_asr_body(word))

    client = _client(chunk_duration_s=10, asr_max_workers=1)
    assert client.transcribe(_wav(seconds=25), "en") == "one two three"


@responses.activate
def test_asr_tolerates_one_failed_chunk():
    """A failed chunk loses a few seconds of speech; failing the whole recording
    loses all of it, and the user can see and fix a gap in the composer."""
    responses.post(BASE_URL, json=_asr_body("start"))
    responses.post(BASE_URL, status=500)
    responses.post(BASE_URL, json=_asr_body("end"))

    client = _client(chunk_duration_s=10, asr_max_workers=1)
    assert client.transcribe(_wav(seconds=25), "en") == "start end"


@responses.activate
def test_asr_repairs_malformed_json():
    """Dhruva intermittently returns slightly broken JSON -- Mitra depends on
    json-repair here for the same reason."""
    responses.post(
        BASE_URL,
        body='{"pipelineResponse": [{"output": [{"source": "repaired",}]}]}',
        content_type="application/json",
    )
    assert _client()._transcribe_chunk(_wav(1.0), "en") == "repaired"


def test_unsupported_language_is_rejected_before_any_request():
    with pytest.raises(UnsupportedLanguageError):
        _client().transcribe(b"whatever", "fr")


# ---------------------------------------------------------------------------
# client -- TTS
# ---------------------------------------------------------------------------

def _tts_body(audio_b64: str) -> dict:
    return {"pipelineResponse": [{"audio": [{"audioContent": audio_b64}]}]}


@responses.activate
def test_tts_sends_only_the_authorization_header():
    responses.post(BASE_URL, json=_tts_body(base64.b64encode(_wav(0.5)).decode()))

    _client().synthesize("hello", "en")

    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == "inference-key"
    assert "userID" not in headers
    assert "ulcaApiKey" not in headers


@responses.activate
def test_tts_strips_markdown_before_synthesising():
    """Otherwise the synthesiser reads asterisks and URLs aloud."""
    responses.post(BASE_URL, json=_tts_body(base64.b64encode(_wav(0.5)).decode()))

    _client().synthesize("**Bold** [link](http://x.example)", "hi")

    import json

    source = json.loads(responses.calls[0].request.body)["inputData"]["input"][0]["source"]
    assert source == "Bold link"


@responses.activate
def test_tts_merges_chunks_over_the_byte_limit_into_one_clip():
    for _ in range(5):
        responses.post(BASE_URL, json=_tts_body(base64.b64encode(_wav(1.0)).decode()))

    result = _client(tts_byte_limit=40).synthesize("Sentence one. " * 20, "en")

    assert result["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(result["audio"])), "rb") as w:
        assert w.getnframes() > 16000  # more than one chunk's worth


@responses.activate
def test_tts_single_chunk_takes_the_fast_path():
    """One request, no decode, no merge -- where most replies land."""
    audio = base64.b64encode(_wav(0.5)).decode()
    responses.post(BASE_URL, json=_tts_body(audio))

    assert _client().synthesize("short", "en")["audio"] == audio
    assert len(responses.calls) == 1


def test_tts_rejects_text_with_nothing_speakable():
    with pytest.raises(BhashiniUpstreamError):
        _client().synthesize("```\ncode only\n```", "en")


# ---------------------------------------------------------------------------
# client -- translation and error mapping
# ---------------------------------------------------------------------------

@responses.activate
def test_translate_returns_the_target_text():
    responses.post(
        BASE_URL,
        json={"pipelineResponse": [{"output": [{"target": "How are you"}]}]},
    )
    assert _client().translate("आप कैसे हैं", "hi", "en") == "How are you"


def test_translate_short_circuits_when_languages_match():
    assert _client().translate("unchanged", "en", "en") == "unchanged"


@responses.activate
def test_translate_raises_instead_of_silently_passing_input_through():
    """Mitra returns `{'status': 200, 'content': <the untranslated input>}` on
    failure (text_to_text.py:64-67), so a failed translation is
    indistinguishable from a successful one and the LLM silently receives
    Hindi. This must raise."""
    responses.post(BASE_URL, status=502)
    with pytest.raises(BhashiniUpstreamError):
        _client().translate("आप कैसे हैं", "hi", "en")


@responses.activate
def test_a_timeout_is_its_own_exception():
    """Mapped to 504 UPSTREAM_TIMEOUT, which the frontend already renders with a
    retry button."""
    responses.post(BASE_URL, body=__import__("requests").exceptions.ConnectTimeout())
    with pytest.raises(BhashiniTimeout):
        _client()._transcribe_chunk(_wav(1.0), "en")


@responses.activate
def test_an_unexpected_body_shape_is_an_upstream_error():
    responses.post(BASE_URL, json={"unexpected": "shape"})
    with pytest.raises(BhashiniUpstreamError) as exc:
        _client()._transcribe_chunk(_wav(1.0), "en")
    assert "pipelineResponse" in str(exc.value)


def test_repr_cannot_leak_credentials():
    text = repr(_client())
    assert "inference-key" not in text
    assert "ulca-key" not in text
    assert "ulca-user" not in text
