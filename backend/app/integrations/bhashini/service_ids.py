"""Bhashini service ids and supported languages.

Responsible for: the per-language, per-task model ids Dhruva expects.
Used by: BhashiniClient, on every request.

A third-party contract: these change when Bhashini publishes new models.
"""
from __future__ import annotations

from typing import Dict

# Sample rates. ASR is fixed by what we send it (ffmpeg resamples to 16 kHz);
# TTS is a request for what Bhashini should synthesise at.
ASR_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 22050

# Whisper for English, a Hindi-specific conformer for Hindi, and the two
# multilingual conformers by language family for the rest.
ASR_SERVICE_IDS: Dict[str, str] = {
    "en": "ai4bharat/whisper-medium-en--gpu--t4",
    "hi": "ai4bharat/conformer-hi-gpu--t4",
    "kn": "ai4bharat/conformer-multilingual-dravidian-gpu--t4",
    "ml": "ai4bharat/conformer-multilingual-dravidian-gpu--t4",
    "ta": "ai4bharat/conformer-multilingual-dravidian-gpu--t4",
    "te": "ai4bharat/conformer-multilingual-dravidian-gpu--t4",
    "bn": "ai4bharat/conformer-multilingual-indo_aryan-gpu--t4",
    "gu": "ai4bharat/conformer-multilingual-indo_aryan-gpu--t4",
    "mr": "ai4bharat/conformer-multilingual-indo_aryan-gpu--t4",
    "or": "ai4bharat/conformer-multilingual-indo_aryan-gpu--t4",
    "pa": "ai4bharat/conformer-multilingual-indo_aryan-gpu--t4",
}

# Coqui TTS, split the same way by family. "misc" carries English.
TTS_SERVICE_IDS: Dict[str, str] = {
    "en": "ai4bharat/indic-tts-coqui-misc-gpu--t4",
    "hi": "ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4",
    "kn": "ai4bharat/indic-tts-coqui-dravidian-gpu--t4",
    "ml": "ai4bharat/indic-tts-coqui-dravidian-gpu--t4",
    "ta": "ai4bharat/indic-tts-coqui-dravidian-gpu--t4",
    "te": "ai4bharat/indic-tts-coqui-dravidian-gpu--t4",
    "as": "ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4",
    "bn": "ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4",
    "gu": "ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4",
    "mr": "ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4",
    "or": "ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4",
    "pa": "ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4",
}

# One model covers every direction, which is why the 1,800 lines of per-pair
# translation entries in the snapshot collapse to a constant.
TRANSLATION_SERVICE_ID = "ai4bharat/indictrans-v2-all-gpu--t4"

# The four the app exposes -- kept in step with the CHECK constraint on
# `conversations.locale` and `agent_sessions.language` (app/models/orm.py).
SUPPORTED_LANGUAGES = ("en", "hi", "kn", "te")

# Bhashini takes bare ISO-639-1 codes ("hi"), NOT the BCP-47 tags Google and
# Sarvam want ("hi-IN"). Mitra's LanguageMapping exists for those providers and
# is deliberately not applied on this path.
