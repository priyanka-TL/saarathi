"""Audio normalisation, chunking and merging.

Bhashini's ASR wants 16 kHz mono PCM WAV; browsers record WebM/Opus (or MP4/AAC
on Safari). Everything in this module exists to bridge that gap, and it is a
port of Mitra's `chatbot/utils/audio_converter_utils.py`,
`chatbot/translate/base/speech_to_text.py` and the merge helpers in
`chatbot/utils/audio_provider_utils.py`.

FIVE THINGS ARE DELIBERATELY DIFFERENT FROM MITRA:

1. The input extension is not forced. Mitra hardcodes `input_ext = 'opus'`
   (audio_converter_utils.py:34), overriding the extension it just sniffed,
   because every upload there is a WhatsApp voice note. Saarthi has Safari
   clients sending MP4, so the container is left for ffmpeg to detect -- which
   it does from content, making the extension advisory anyway.
2. `subprocess.run` has a timeout. Without one a wedged ffmpeg holds a worker
   thread and its database connection forever, and THREADPOOL_SIZE is small.
3. stderr is captured. `check=True` alone raises `CalledProcessError` with no
   detail, so a malformed upload and a missing codec look identical in the log.
4. Temp files live in a `TemporaryDirectory`, so cleanup cannot miss one. Mitra
   guards `os.remove` with `'input_path' in locals()`, which silently skips
   cleanup if the failure happened one line too early.
5. No silence detection. Mitra drops chunks under -40 dBFS using pydub; the
   browser already applies an RMS gate before uploading at all (Mitra's own
   frontend does this too), so the dependency buys nothing here.
"""
from __future__ import annotations

import base64
import io
import logging
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import List, Tuple

from app.integrations.bhashini.exceptions import AudioConversionError

logger = logging.getLogger(__name__)

# 16 kHz mono is what every Bhashini ASR service expects; sending anything else
# gets a transcript back, just a worse one.
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1


def ffmpeg_available() -> bool:
    """Whether the ffmpeg binary is on PATH.

    Checked at container build time so a missing binary is a startup warning
    rather than a 502 on a user's first recording.
    """
    return shutil.which("ffmpeg") is not None


def convert_to_wav(audio: bytes, timeout_s: float = 30.0) -> bytes:
    """Transcode arbitrary browser audio to 16 kHz mono PCM WAV.

    Accepts WebM/Opus, MP4/AAC, Ogg, WAV -- anything ffmpeg can demux. The
    container is detected from content, so the caller does not have to know
    what the browser chose.
    """
    if not audio:
        raise AudioConversionError("no audio data to convert")
    if not ffmpeg_available():
        raise AudioConversionError(
            "ffmpeg is not installed or not on PATH. It is a system dependency "
            "of the voice feature -- install it with `brew install ffmpeg` or "
            "`apt-get install -y ffmpeg`."
        )

    with tempfile.TemporaryDirectory(prefix="saarthi-voice-") as tmp:
        # No suffix: ffmpeg identifies the container from content, and guessing
        # wrong here would be worse than not guessing.
        src = Path(tmp) / "input"
        dst = Path(tmp) / "output.wav"
        src.write_bytes(audio)

        command = [
            "ffmpeg",
            "-y",
            "-i", str(src),
            "-ac", str(TARGET_CHANNELS),
            "-ar", str(TARGET_SAMPLE_RATE),
            # Explicit codec: ffmpeg's default for .wav is already pcm_s16le,
            # but `wave` cannot read anything else, so state it rather than
            # depending on a default.
            "-acodec", "pcm_s16le",
            str(dst),
        ]
        # An argument LIST, never shell=True -- this runs on untrusted input.
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioConversionError(f"ffmpeg timed out after {timeout_s}s") from exc
        except OSError as exc:
            raise AudioConversionError(f"could not run ffmpeg: {exc}") from exc

        if result.returncode != 0:
            # ffmpeg's last stderr line names the actual problem ("Invalid data
            # found when processing input", "Unknown encoder"); the rest is
            # banner noise.
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()
            last_line = detail.splitlines()[-1] if detail else "no stderr"
            raise AudioConversionError(f"ffmpeg failed (exit {result.returncode}): {last_line}")

        try:
            wav = dst.read_bytes()
        except OSError as exc:
            raise AudioConversionError(f"ffmpeg produced no output: {exc}") from exc

    if not wav:
        raise AudioConversionError("ffmpeg produced an empty file")
    return wav


def split_audio(wav_bytes: bytes, chunk_duration_s: int = 10) -> List[Tuple[int, bytes]]:
    """Split a WAV into self-contained WAV chunks of at most `chunk_duration_s`.

    Each chunk carries its own RIFF header with the source's channel count,
    sample width and frame rate, so it can be base64-encoded and sent to
    Bhashini directly. Chunking exists because Dhruva's synchronous ASR degrades
    badly on long clips.

    Input MUST be real PCM WAV -- `convert_to_wav` guarantees that.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as source:
            frame_rate = source.getframerate()
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            total_frames = source.getnframes()
            chunk_frames = max(1, chunk_duration_s * frame_rate)

            chunks: List[Tuple[int, bytes]] = []
            position = 0
            index = 0

            while position < total_frames:
                size = min(chunk_frames, total_frames - position)
                source.setpos(position)
                frames = source.readframes(size)

                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as chunk:
                    chunk.setnchannels(channels)
                    chunk.setsampwidth(sample_width)
                    chunk.setframerate(frame_rate)
                    chunk.writeframes(frames)

                chunks.append((index, buffer.getvalue()))
                position += size
                index += 1
    except wave.Error as exc:
        raise AudioConversionError(f"not a readable PCM WAV: {exc}") from exc

    if not chunks:
        raise AudioConversionError("audio contains no frames")
    return chunks


def detect_audio_format(audio: bytes) -> str:
    """'wav' for a RIFF header, otherwise 'mp3'.

    Bhashini does not declare what TTS returns, so the format is sniffed rather
    than assumed -- which is also why the two merge strategies below have to be
    chosen at runtime.
    """
    return "wav" if audio[:4] == b"RIFF" else "mp3"


def merge_audio_base64(chunks_b64: List[str], audio_format: str) -> str:
    """Concatenate base64 audio chunks into one base64 clip.

    WAV needs the stdlib `wave` writer: raw concatenation would embed a RIFF
    header in the middle of the stream and every player stops at the first one.
    MP3 frames are independently decodable, so a byte join is correct there.
    """
    if not chunks_b64:
        raise AudioConversionError("no audio chunks to merge")
    if len(chunks_b64) == 1:
        return chunks_b64[0]

    try:
        raw = [base64.b64decode(c) for c in chunks_b64]

        if audio_format == "wav":
            output = io.BytesIO()
            with wave.open(output, "wb") as out:
                params_set = False
                for chunk in raw:
                    with wave.open(io.BytesIO(chunk), "rb") as src:
                        if not params_set:
                            # Take channels/width/rate from the first chunk; every
                            # chunk came from one TTS voice, so they agree.
                            out.setparams(src.getparams())
                            params_set = True
                        out.writeframes(src.readframes(src.getnframes()))
            return base64.b64encode(output.getvalue()).decode("utf-8")

        return base64.b64encode(b"".join(raw)).decode("utf-8")
    except AudioConversionError:
        raise
    except Exception as exc:
        raise AudioConversionError(f"could not merge audio chunks: {exc}") from exc
