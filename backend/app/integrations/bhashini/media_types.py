"""Audio content types the voice feature accepts, and their file extensions.

Lives beside the speech client rather than in the router because it describes
what the AUDIO PIPELINE can ingest, not how one HTTP route behaves. The set is
bounded by what ffmpeg (`audio.py`) transcodes and what browsers actually record
in -- `MediaRecorder` produces webm/ogg on Chromium and Firefox and mp4 on
Safari.
"""
from __future__ import annotations

#: content type -> extension used in the object key.
#:
#: The extension is COSMETIC. ffmpeg detects the container from the bytes, so a
#: wrong guess costs nothing at transcode time; the extension exists to make a
#: key readable in a bucket listing.
CONTENT_TYPE_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}

#: Used when the client sends a content type not in the map above.
DEFAULT_EXTENSION = "webm"

#: What a client is assumed to be uploading when it names no content type.
DEFAULT_CONTENT_TYPE = "audio/webm"


def extension_for(content_type: str) -> str:
    """The object-key extension for `content_type`, falling back to the default."""
    return CONTENT_TYPE_EXTENSIONS.get(content_type, DEFAULT_EXTENSION)
