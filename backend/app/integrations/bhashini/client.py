"""HTTP client for Bhashini (AI4Bharat / ULCA Dhruva).

One endpoint serves every task. The `taskType` in `pipelineTasks[0]` selects
between ASR, TTS and translation, and the response is shaped differently for
each -- which is why the three public methods do not share a parser.

SYNCHRONOUS, with one reusable `requests.Session`, matching
`integrations/mitra/rest_client.py`. Endpoints are plain `def` and Starlette
already runs them in a worker thread; an async client would need the endpoint to
be `async def`, which this codebase forbids for good reasons (CLAUDE.md
§ Patterns).

NO RETRIES, deliberately. ASR requests carry ten seconds of audio and take
seconds to answer, so a blind retry doubles the wall time a user waits before
being told it failed -- and a worker thread is held for all of it. The frontend
decides whether to retry, because only it knows the user is still there.

THE AUTH HEADERS ARE ASYMMETRIC, and that is faithful to Mitra: ASR sends
`Authorization` + `userID` + `ulcaApiKey`, while TTS and translation send
`Authorization` alone. Dhruva accepts both shapes. Sending all three everywhere
would probably work, but "probably" against an undocumented public service is
not worth the change.
"""
from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests
from json_repair import repair_json

from app.integrations.bhashini.audio import (
    convert_to_wav,
    detect_audio_format,
    merge_audio_base64,
    split_audio,
)
from app.integrations.bhashini.exceptions import (
    BhashiniTimeout,
    BhashiniUpstreamError,
    UnsupportedLanguageError,
)
from app.integrations.bhashini.service_ids import (
    ASR_SAMPLE_RATE,
    ASR_SERVICE_IDS,
    SUPPORTED_LANGUAGES,
    TRANSLATION_SERVICE_ID,
    TTS_SAMPLE_RATE,
    TTS_SERVICE_IDS,
)
from app.integrations.bhashini.text import split_text_for_tts, strip_markdown_for_tts

logger = logging.getLogger(__name__)


class BhashiniClient:
    """Speech-to-text, text-to-speech and translation over Bhashini."""

    def __init__(
        self,
        *,
        base_url: str,
        authorization: str,
        api_key: Optional[str] = None,
        user_id: Optional[str] = None,
        # NO DEFAULTS on the tuning knobs, deliberately. Every one of these has
        # a Settings field behind it (bhashini_*_timeout, voice_chunk_duration_s,
        # voice_tts_byte_limit, voice_asr_max_workers) and build_container()
        # passes all five. A default here would be a SECOND declaration of the
        # same number, free to drift from the one operators actually tune -- and
        # the drift would be invisible, because the injected value would keep
        # winning in production while tests exercised the stale one.
        connect_timeout: float,
        read_timeout: float,
        chunk_duration_s: int,
        tts_byte_limit: int,
        asr_max_workers: int,
        ffmpeg_timeout_s: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._authorization = authorization
        self._api_key = api_key
        self._user_id = user_id
        self._timeout: Tuple[float, float] = (connect_timeout, read_timeout)
        self.chunk_duration_s = chunk_duration_s
        self.tts_byte_limit = tts_byte_limit
        self.asr_max_workers = max(1, asr_max_workers)
        self.ffmpeg_timeout_s = ffmpeg_timeout_s
        self._session = requests.Session()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Explicit: the default would print _authorization, _api_key and _user_id.
        return f"<BhashiniClient base_url={self.base_url}>"

    # ---- public API -------------------------------------------------------

    def transcribe(self, audio: bytes, language: str) -> str:
        """Transcribe browser audio. Returns the transcript, possibly empty.

        Takes raw uploaded bytes in whatever container the browser produced,
        normalises to 16 kHz mono WAV, splits into chunks and transcribes them
        in parallel. An empty string is a valid answer -- silence transcribes to
        nothing, and the caller decides whether that is worth an error.
        """
        self._check_language(language, ASR_SERVICE_IDS)

        wav = convert_to_wav(audio, self.ffmpeg_timeout_s)
        chunks = split_audio(wav, self.chunk_duration_s)
        logger.info("asr: %d chunk(s), language=%s", len(chunks), language)

        if len(chunks) == 1:
            return self._transcribe_chunk(chunks[0][1], language).strip()

        # Bounded, unlike Mitra's bare ThreadPoolExecutor() -- a five-minute
        # recording there fans out ~30 simultaneous requests at Dhruva.
        with ThreadPoolExecutor(max_workers=self.asr_max_workers) as pool:
            futures = {
                index: pool.submit(self._transcribe_chunk, data, language)
                for index, data in chunks
            }
            # Reassembled by chunk index, NOT completion order -- the whole
            # point of numbering them.
            parts: List[str] = []
            for index in sorted(futures):
                try:
                    parts.append(futures[index].result())
                except Exception as exc:
                    # One failed chunk loses a few seconds of speech; failing the
                    # whole recording loses all of it, and the user can see and
                    # correct a gap in the composer.
                    logger.warning("asr: chunk %d failed (%s)", index, type(exc).__name__)
                    parts.append("")

        return " ".join(part for part in parts if part).strip()

    def synthesize(self, text: str, language: str, gender: str = "female") -> Dict[str, str]:
        """Synthesise speech. Returns `{"audio": <base64>, "format": "wav"|"mp3"}`.

        Strips markdown first, then splits text over the byte limit and merges
        the resulting clips.
        """
        self._check_language(language, TTS_SERVICE_IDS)

        clean = strip_markdown_for_tts(text)
        if not clean:
            raise BhashiniUpstreamError("tts", detail="no speakable text after markdown removal")

        chunks = split_text_for_tts(clean, self.tts_byte_limit)

        # Fast path: one request, no decode, no merge. Most replies land here.
        if len(chunks) == 1:
            audio_b64 = self._synthesize_chunk(chunks[0], language, gender)
            return {"audio": audio_b64, "format": self._sniff(audio_b64)}

        logger.info("tts: %d chunk(s), language=%s", len(chunks), language)
        # SEQUENTIAL, unlike ASR: these are concatenated into one clip, so the
        # ordering has to be exact and there is nothing to gain from racing.
        parts = [self._synthesize_chunk(chunk, language, gender) for chunk in chunks]
        audio_format = self._sniff(parts[0])
        return {"audio": merge_audio_base64(parts, audio_format), "format": audio_format}

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        """Translate text. Returns the translation."""
        if source_language == target_language:
            return text

        payload = {
            "pipelineTasks": [
                {
                    "taskType": "translation",
                    "config": {
                        "language": {
                            "sourceLanguage": source_language,
                            "targetLanguage": target_language,
                        },
                        "serviceId": TRANSLATION_SERVICE_ID,
                    },
                }
            ],
            "inputData": {"input": [{"source": text}]},
        }
        data = self._post("translation", payload, with_ulca_headers=False)
        # Mitra returns `{'status': 200, 'content': <the untranslated input>}`
        # here when the response is unexpected (text_to_text.py:64-67), so a
        # failed translation is indistinguishable from a successful one and the
        # LLM silently receives Hindi. This raises instead.
        return self._first(data, "translation", "output", "target")

    # ---- per-task calls ---------------------------------------------------

    def _transcribe_chunk(self, wav_chunk: bytes, language: str) -> str:
        payload = {
            "pipelineTasks": [
                {
                    "taskType": "asr",
                    "config": {
                        "language": {"sourceLanguage": language},
                        "serviceId": ASR_SERVICE_IDS[language],
                        "audioFormat": "wav",
                        "samplingRate": ASR_SAMPLE_RATE,
                        "preProcessors": [],
                        "postProcessors": [],
                    },
                }
            ],
            "inputData": {
                "audio": [{"audioContent": base64.b64encode(wav_chunk).decode("utf-8")}]
            },
        }
        data = self._post("asr", payload, with_ulca_headers=True)
        return self._first(data, "asr", "output", "source")

    def _synthesize_chunk(self, text: str, language: str, gender: str) -> str:
        payload = {
            "pipelineTasks": [
                {
                    "taskType": "tts",
                    "config": {
                        "language": {"sourceLanguage": language},
                        "gender": gender.lower(),
                        "serviceId": TTS_SERVICE_IDS[language],
                        "samplingRate": TTS_SAMPLE_RATE,
                    },
                }
            ],
            "inputData": {"input": [{"source": text}]},
        }
        data = self._post("tts", payload, with_ulca_headers=False)
        # TTS answers under "audio"/"audioContent"; ASR and translation answer
        # under "output". Same envelope, different leaf.
        return self._first(data, "tts", "audio", "audioContent")

    # ---- transport --------------------------------------------------------

    def _post(self, task: str, payload: dict, *, with_ulca_headers: bool) -> Any:
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "Authorization": self._authorization,
        }
        if with_ulca_headers:
            if self._user_id:
                headers["userID"] = self._user_id
            if self._api_key:
                headers["ulcaApiKey"] = self._api_key

        try:
            response = self._session.post(
                self.base_url, json=payload, headers=headers, timeout=self._timeout
            )
        except requests.exceptions.Timeout as exc:
            raise BhashiniTimeout(task, self._timeout[1]) from exc
        except requests.exceptions.RequestException as exc:
            raise BhashiniUpstreamError(task, detail=type(exc).__name__) from exc

        if response.status_code != 200:
            raise BhashiniUpstreamError(task, status=response.status_code)

        # repair_json, not response.json(): Dhruva intermittently returns
        # slightly malformed JSON, and Mitra hit this often enough to depend on
        # the repair (speech_to_text.py). json-repair is already a dependency.
        try:
            return repair_json(response.text, return_objects=True)
        except Exception as exc:
            raise BhashiniUpstreamError(task, status=200, detail="unparseable body") from exc

    @staticmethod
    def _first(data: Any, task: str, container: str, leaf: str) -> str:
        """Pull `pipelineResponse[0][container][0][leaf]` out of a response.

        Every task shares this envelope and differs only in the last two names,
        so one careful walk beats three copies of the same nested indexing.
        """
        if not isinstance(data, dict) or "pipelineResponse" not in data:
            raise BhashiniUpstreamError(task, status=200, detail="no pipelineResponse in body")
        try:
            return str(data["pipelineResponse"][0][container][0][leaf])
        except (KeyError, IndexError, TypeError) as exc:
            raise BhashiniUpstreamError(
                task, status=200, detail=f"missing {container}[0].{leaf}"
            ) from exc

    @staticmethod
    def _sniff(audio_b64: str) -> str:
        """Detect the container of a base64 clip.

        Bhashini does not declare what it returns, and the answer decides which
        merge strategy is correct, so only the header bytes are decoded.

        Exactly 8 base64 characters: a multiple of 4, so it decodes without
        padding, and it yields 6 bytes -- two more than the 4 the RIFF magic
        needs.
        """
        try:
            return detect_audio_format(base64.b64decode(audio_b64[:8]))
        except Exception:
            # An undecodable prefix means the merge path would fail anyway; wav
            # is the format Bhashini/IITM/TTS actually returns at 22.05 kHz.
            return "wav"

    @staticmethod
    def _check_language(language: str, table: Dict[str, str]) -> None:
        if language not in table:
            raise UnsupportedLanguageError(language, list(SUPPORTED_LANGUAGES))
