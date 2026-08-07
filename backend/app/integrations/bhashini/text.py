"""Text preparation for speech synthesis.

Responsible for: stripping markdown, and splitting text to the TTS byte limit.
Used by: BhashiniClient.synthesize, on every reply read aloud.

THE REGEX ORDER IS LOAD-BEARING -- four steps only work where they are, and the
failure mode is a stray character or a lost word, never an exception:

  * images before links, or `![alt](url)` leaves a stray `!`
  * `<br>` -> ". " before the general tag strip, or the pause is lost
  * the doubled-period cleanup AFTER that, or text already ending in
    punctuation reads ".."
  * bullet markers before bold/italic, or `* **bold**` loses its first word

Do not reorder them to group "similar" rules together.
"""
from __future__ import annotations

import logging
import re
from typing import List

logger = logging.getLogger(__name__)


def strip_markdown_for_tts(text: str) -> str:
    """Strip markdown so a TTS engine receives clean natural-language text.

    Without this the synthesiser reads asterisks, pipes and URLs aloud. Agent
    replies are markdown by construction (the frontend renders them through
    marked), so every reply hits this.
    """
    if text is None:
        return ""
    if not text:
        return text

    # Fenced code blocks -- remove entirely (before anything else to avoid inner matches)
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Inline code -- remove
    text = re.sub(r'`[^`\n]+`', '', text)
    # Images first -- must precede links or [alt](url) gets consumed leaving a stray !
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)
    # Links -- keep display text, drop URL
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    # Horizontal rules: standalone line of 3+ hyphens / underscores / asterisks
    text = re.sub(r'^\s*[-_*]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Multiple consecutive hyphens used as em/en dash
    text = re.sub(r'-{2,}', ' ', text)
    # Table separator rows (|---|:---|)
    text = re.sub(r'^\|[\s\-|:]+\|$', '', text, flags=re.MULTILINE)
    # Table cell pipes -> space
    text = re.sub(r'\|', ' ', text)
    # Heading markers (# / ## / ### etc.)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Blockquote markers
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    # HTML line breaks -> period so TTS pauses between items (LLM uses <br> inside table cells)
    text = re.sub(r'\s*<br\s*/?>\s*', '. ', text, flags=re.IGNORECASE)
    # Any remaining HTML tags -> remove
    text = re.sub(r'<[^>]+>', '', text)
    # Unicode bullet character -> remove (period from <br> already provides the pause)
    text = re.sub(r'•\s*', '', text)
    # Clean up double periods that arise when text before <br> already ended with punctuation
    text = re.sub(r'([.!?])\s*\.\s*', r'\1 ', text)
    # Bullet list markers BEFORE bold/italic -- prevents * bullet + **bold** being misread as nested *{1,3}
    text = re.sub(r'^[ \t]*[-*]\s+', '', text, flags=re.MULTILINE)
    # Bold + italic with asterisks: ***text*** / **text** / *text*  (single-line, non-nested)
    text = re.sub(r'\*{1,3}([^\n*]*?)\*{1,3}', r'\1', text)
    # Bold + italic with underscores: __text__ / _text_  (single-line, non-nested)
    text = re.sub(r'_{1,2}([^\n_]*?)_{1,2}', r'\1', text)
    # Remaining lone asterisks / underscores not attached to word characters
    text = re.sub(r'(?<!\w)[*_]+(?!\w)', '', text)
    # "1: 30" is read as a time by TTS engines -- replace colon with comma
    text = re.sub(r'(?<!\d)(\d+)\s*:\s*(\d+)(?!\d)', r'\1, \2', text)
    # Collapse 3+ consecutive newlines -> 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Collapse multiple spaces/tabs -> single space
    text = re.sub(r'[ \t]{2,}', ' ', text)

    return text.strip()


def split_on_words(text: str, byte_limit: int) -> List[str]:
    """Split on word boundaries when a single sentence exceeds byte_limit.

    The per-character fallback is not paranoia: the limit is in UTF-8 BYTES, and
    one Devanagari character is three of them, so a long compound word in Hindi
    can exceed a limit that its character count suggests is nowhere near.
    """
    try:
        words = text.split()
        chunks: List[str] = []
        current = ""
        for word in words:
            if len(word.encode('utf-8')) > byte_limit:
                if current:
                    chunks.append(current)
                    current = ""
                part = ""
                for ch in word:
                    candidate = f"{part}{ch}"
                    if len(candidate.encode('utf-8')) <= byte_limit:
                        part = candidate
                    else:
                        if part:
                            chunks.append(part)
                        part = ch
                if part:
                    chunks.append(part)
                continue
            candidate = f"{current} {word}".strip() if current else word
            if len(candidate.encode('utf-8')) <= byte_limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = word
        if current:
            chunks.append(current)
        return chunks or [text]
    except Exception as exc:
        logger.error("TTS word split failed, returning text as-is: %s", exc)
        return [text]


def split_text_for_tts(text: str, byte_limit: int) -> List[str]:
    """Split into byte-safe chunks at sentence boundaries, then word boundaries.

    The sentence regex includes the Devanagari danda (U+0964) alongside `.?!`,
    without which Hindi text has no sentence boundaries at all and every reply
    falls through to the word splitter.
    """
    try:
        if len(text.encode('utf-8')) <= byte_limit:
            return [text]

        # Sentence boundaries (।, ., ?, !) and paragraph breaks, keeping delimiters
        sentences = re.split(r'(?<=[।.?!])\s+|\n\n+', text)
        sentences = [s.strip() for s in sentences if s.strip()]

        chunks: List[str] = []
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}".strip() if current else sentence
            if len(candidate.encode('utf-8')) <= byte_limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                if len(sentence.encode('utf-8')) > byte_limit:
                    word_chunks = split_on_words(sentence, byte_limit)
                    chunks.extend(word_chunks[:-1])
                    current = word_chunks[-1] if word_chunks else ""
                else:
                    current = sentence

        if current:
            chunks.append(current)

        return chunks or [text]
    except Exception as exc:
        logger.error("TTS text split failed, returning text as single chunk: %s", exc)
        return [text]
