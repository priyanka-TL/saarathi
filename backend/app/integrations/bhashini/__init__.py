"""Bhashini (AI4Bharat / ULCA Dhruva) speech services.

Ported from Mitra's `chatbot/translate/ai4Bharat/` and the pure helpers in
`chatbot/utils/audio_provider_utils.py`, minus the Django models -- Mitra
resolves a provider per bot per language from a `Voice` table, whereas Saarthi
speaks to exactly one provider and reads its configuration from `.env`.
"""
from app.integrations.bhashini.client import BhashiniClient
from app.integrations.bhashini.exceptions import (
    AudioConversionError,
    BhashiniError,
    BhashiniTimeout,
    BhashiniUpstreamError,
    UnsupportedLanguageError,
)
from app.integrations.bhashini.service_ids import SUPPORTED_LANGUAGES

__all__ = [
    "AudioConversionError",
    "BhashiniClient",
    "BhashiniError",
    "BhashiniTimeout",
    "BhashiniUpstreamError",
    "SUPPORTED_LANGUAGES",
    "UnsupportedLanguageError",
]
