"""Parsing a JSON-framed conversational WebSocket.

Responsible for: turning a raw frame into the normalised `Frame` the channel
acts on.
Used by: `BaseWsFlowProvider` hands this to `WsChannel`'s reader thread.

The frame vocabulary of the platforms this serves is not versioned and varies by
flow, so parsing is tolerant by design: an unrecognised frame becomes an
ignorable event rather than an error, because raising in the reader thread would
kill the socket.

OPTION SHAPES ARE EXTENSIBLE PER PROVIDER. The `extra_content` shapes below are
the ones common to this protocol family; a platform that offers choices in a
shape of its own passes an extra reader rather than editing this file. That is
what keeps a platform-specific quirk out of the shared parser -- see
`app/providers/saathi/frames.py` for the worked example.

`extra_content` CARRIES TWO INDEPENDENT KINDS OF THING, read by two separate
functions: choice buttons (`_normalise_options`) and downloadable documents
(`_normalise_attachments`). They are parsed independently and never compete, so
a frame offering both a plan to download AND a follow-up question yields both.
"""
from __future__ import annotations

import json
from posixpath import basename
from typing import Callable, Optional, Sequence
from urllib.parse import unquote, urlparse

from app.providers.transport.frames import Attachment, Frame, FrameKind, ParsedOption

#: A reader takes the raw `extra_content` dict and returns options, or None to
#: decline (letting the next reader, or the shared shapes, try).
OptionReader = Callable[[dict], Optional[list]]

#: The same seam, for downloadable documents.
AttachmentReader = Callable[[dict], Optional[list]]

#: Suffix that marks a download key. `pdf_url` -> format "pdf".
_URL_SUFFIX = "_url"

#: Known formats. Anything else still becomes an attachment -- a platform that
#: starts sending `pptx_url` needs no code change, it just gets the generic
#: media type until someone adds a row here.
MEDIA_TYPES = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc":  "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv":  "text/csv",
    "txt":  "text/plain",
}
DEFAULT_MEDIA_TYPE = "application/octet-stream"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_str(v: object, default: str = "") -> str:
    """Return v as str; return default on None."""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return str(v)


def _recover_response_text(obj: object, _depth: int = 0) -> str:
    """Pull any user-facing ``response`` strings out of a leaked LLM payload.

    These platforms' own handlers treat the ``response`` key as the text meant
    for the user, so when the leaked object carries a non-empty one that IS the
    reply and is worth showing. In the observed case it was ``''`` (the model put
    everything in the tool call), which is why this returns "" rather than
    inventing text.

    Depth-capped: this walks a payload from an upstream service, and a
    self-referential or pathologically nested one must not blow the stack.
    """
    if _depth > 6:
        return ""

    if isinstance(obj, dict):
        parts = []
        value = obj.get("response")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, (dict, list)):
            nested = _recover_response_text(value, _depth + 1)
            if nested:
                parts.append(nested)
        return " ".join(parts)

    if isinstance(obj, list):
        parts = [_recover_response_text(item, _depth + 1) for item in obj]
        return " ".join(p for p in parts if p)

    return ""


def _coerce_message_text(v: object) -> tuple[str, bool]:
    """Return ``(text, is_control_payload)`` for a raw ``msg`` value.

    ``msg`` is a string on every legitimate frame. A list/dict means the platform
    leaked an internal LLM object -- str()ing it is what printed
    ``[{'response': '', 'name': 'get_state_information', ...}]`` into a chat
    bubble. Structure is the signal here, deliberately: matching on substrings
    like "get_state_information" would be guesswork that could also swallow a
    legitimate reply which happens to quote one.
    """
    if v is None:
        return "", False
    if isinstance(v, str):
        return v, False
    if isinstance(v, (int, float, bool)):
        # Scalar: str() is meaningful, keep the old lenient behaviour.
        return str(v), False
    return _recover_response_text(v), True


def _coerce_int(v: object, default: int = 0) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _normalise_options(
    extra_content: object, option_readers: Sequence[OptionReader] = (),
) -> list[ParsedOption]:
    """Map the ``extra_content`` shapes to a flat ``ParsedOption`` list.

    Shape 1 -- state-machine: ``{question: str, options: list[dict]}``
      Each dict may carry keys: id, label, value, text (use whichever exist).

    Shape 2 -- story validation: ``{problem_statement, should_move_forward, validation}``
      No user-facing choices; yields an empty list.
      (should_move_forward is a machine signal, not a UI affordance.)

    Shape 3 -- RAG sources: ``{sources: list[dict]}``
      Each source has url/title/text; surface as tappable citations.

    Provider-specific shapes are supplied through ``option_readers`` and are
    tried FIRST, so a platform can claim a key the shared shapes would otherwise
    ignore without editing this function.
    """
    if not isinstance(extra_content, dict):
        return []

    for reader in option_readers:
        claimed = reader(extra_content)
        if claimed is not None:
            return claimed

    # Shape 1 -- state machine choices
    if "options" in extra_content:
        raw_opts = extra_content["options"]
        if isinstance(raw_opts, list):
            result = []
            for i, item in enumerate(raw_opts):
                if not isinstance(item, dict):
                    continue
                # Prefer explicit id -> fall back to ordinal string
                opt_id    = _coerce_str(item.get("id") or item.get("value"), str(i))
                opt_label = _coerce_str(
                    item.get("label") or item.get("text") or item.get("value"), opt_id
                )
                opt_value = _coerce_str(item.get("value") or item.get("label"), opt_id)
                result.append(ParsedOption(id=opt_id, label=opt_label, value=opt_value))
            return result

    # Shape 2 -- story validation (machine signal, no UI choices)
    if "problem_statement" in extra_content:
        return []

    # Shape 3 -- free-flow RAG sources -> surface as citations
    if "sources" in extra_content:
        raw_sources = extra_content["sources"]
        if isinstance(raw_sources, list):
            result = []
            for i, item in enumerate(raw_sources):
                if not isinstance(item, dict):
                    continue
                src_id    = _coerce_str(item.get("url") or item.get("id"), str(i))
                src_label = _coerce_str(
                    item.get("title") or item.get("text") or item.get("url"), src_id
                )
                src_value = _coerce_str(item.get("url") or item.get("text"), src_id)
                result.append(ParsedOption(id=src_id, label=src_label, value=src_value))
            return result

    return []


def _file_name_from_url(url: str) -> str:
    """A document name derived from the URL, for a payload that omits one.

    Strips the query string, percent-decodes, and drops the extension -- the
    extension is already carried as `format`, and doubling it would produce
    `plan.pdf.pdf` once the two are joined for the download hint.
    """
    stem = basename(unquote(urlparse(url).path))
    return stem.rsplit(".", 1)[0] if "." in stem else stem


def _normalise_attachments(
    extra_content: object, attachment_readers: Sequence[AttachmentReader] = (),
) -> list[Attachment]:
    """Map the ``download`` shape to a flat ``Attachment`` list.

    Shape -- one document, offered in one or more formats::

        {"download": {"pdf_url": "https://…/plan.pdf",
                      "docx_url": "https://…/plan.docx",
                      "file_name": "plan"}}

    EVERY ``<format>_url`` KEY BECOMES AN ATTACHMENT. Reading the format off the
    key rather than checking for `pdf_url` and `docx_url` by name is what makes
    a future `pptx_url` work with no code change -- the same reasoning that
    keeps `flow_name` out of a Literal.

    Tolerant in exactly the way the rest of this module is: a missing, empty,
    non-dict or malformed ``download`` block yields an empty list rather than an
    error, because this runs on the socket reader thread where an exception
    kills the connection. An empty list renders nothing, which is the correct
    outcome for a turn that offers no files.

    Provider-specific shapes go through ``attachment_readers``, tried FIRST,
    exactly as option readers are.
    """
    if not isinstance(extra_content, dict):
        return []

    for reader in attachment_readers:
        claimed = reader(extra_content)
        if claimed is not None:
            return claimed

    download = extra_content.get("download")
    if not isinstance(download, dict):
        return []

    # Not display text -- it names the saved file. Absent is normal; each URL
    # falls back to its own basename below.
    declared_name = download.get("file_name")
    declared_name = declared_name.strip() if isinstance(declared_name, str) else ""

    attachments: list[Attachment] = []
    for key, raw_url in download.items():
        if not key.endswith(_URL_SUFFIX):
            continue
        # A null or non-string URL is how the platform says "this format was
        # not generated", so it is skipped rather than rendered as a dead link.
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue

        url = raw_url.strip()
        fmt = key[: -len(_URL_SUFFIX)].strip().lower()
        if not fmt:
            continue

        attachments.append(Attachment(
            file_name=declared_name or _file_name_from_url(url),
            format=fmt,
            media_type=MEDIA_TYPES.get(fmt, DEFAULT_MEDIA_TYPE),
            url=url,
        ))

    # Deterministic order, so two formats always render in the same sequence
    # rather than following dict order from the wire.
    attachments.sort(key=lambda a: a.format)
    return attachments


def _parse_text_envelope(
    text: dict, option_readers: Sequence[OptionReader],
    attachment_readers: Sequence[AttachmentReader] = (),
) -> Frame:
    """Parse the primary ``{"text": {...}}`` envelope."""
    source        = _coerce_str(text.get("source"))
    msg, control  = _coerce_message_text(text.get("msg"))
    step          = _coerce_int(text.get("step"))
    finish_reason = text.get("finish_reason")   # keep None vs "" distinction
    extra_content = text.get("extra_content")
    error_val     = _coerce_str(text.get("error"))

    # user-echo: source == "user"
    if source == "user":
        return Frame(
            kind=FrameKind.USER_ECHO,
            source=source,
            msg=msg,
            step=step,
            control_payload=control,
        )

    # System / error frame
    if source == "system" or error_val:
        return Frame(
            kind=FrameKind.SYSTEM,
            source=source,
            msg=msg,
            step=step,
            error=error_val or msg,
            control_payload=control,
        )

    # Bot turn -- chunked or final
    #
    # Options and attachments are read INDEPENDENTLY from the same block: a
    # frame may carry choice buttons, downloadable documents, both, or neither.
    options = _normalise_options(extra_content, option_readers)
    attachments = _normalise_attachments(extra_content, attachment_readers)
    if finish_reason:
        kind = FrameKind.BOT_FINAL
    else:
        kind = FrameKind.BOT_CHUNK

    return Frame(
        kind=kind,
        source=source,
        msg=msg,
        step=step,
        finish_reason=_coerce_str(finish_reason) if finish_reason else None,
        options=options,
        attachments=attachments,
        control_payload=control,
    )


# Legacy envelope types seen in the Node bot reference client.
_LEGACY_SYSTEM_TYPES = frozenset({
    "session_end", "error", "connected", "disconnected",
})
_LEGACY_BOT_TYPES = frozenset({
    "message", "bot_response", "response",
})
_LEGACY_OPTION_TYPES = frozenset({
    "options", "choices",
})


def _parse_legacy_envelope(
    raw: dict, option_readers: Sequence[OptionReader],
    attachment_readers: Sequence[AttachmentReader] = (),
) -> Frame:
    """Handle legacy envelope formats that wrap the payload differently.

    The Node bot reference client logs several type values that differ from the
    primary ``{"text": {...}}`` shape. Handle them defensively so the parser
    works across deployment variants.
    """
    envelope_type = _coerce_str(raw.get("type")).lower()
    msg, control  = _coerce_message_text(
        raw.get("msg") or raw.get("message") or raw.get("text") or raw.get("content")
    )
    error_val     = _coerce_str(raw.get("error") or raw.get("err"))

    if envelope_type in _LEGACY_SYSTEM_TYPES:
        return Frame(
            kind=FrameKind.SYSTEM,
            source="system",
            msg=msg,
            error=error_val or (msg if envelope_type == "error" else ""),
            control_payload=control,
        )

    if envelope_type in _LEGACY_OPTION_TYPES:
        # Normalise the choice list the same way as extra_content.options
        raw_opts = raw.get("options") or raw.get("choices") or []
        extra = {"options": raw_opts} if raw_opts else {}
        return Frame(
            kind=FrameKind.BOT_FINAL,
            source="bot",
            msg=msg,
            finish_reason="stop",
            options=_normalise_options(extra, option_readers),
            control_payload=control,
        )

    if envelope_type in _LEGACY_BOT_TYPES or envelope_type == "text":
        finish_reason = _coerce_str(raw.get("finish_reason")) or None
        return Frame(
            kind=FrameKind.BOT_FINAL if finish_reason else FrameKind.BOT_CHUNK,
            source="bot",
            msg=msg,
            finish_reason=finish_reason,
            # A legacy envelope may still carry extra_content; a download there
            # is as real as one on the primary shape.
            attachments=_normalise_attachments(
                raw.get("extra_content"), attachment_readers,
            ),
            control_payload=control,
        )

    # Unknown type -- treat as a system/info frame.
    # Falls back to the raw envelope_type when no explicit `source` is given, so
    # a defensive caller (e.g. WsChannel checking ack_types) has a checkable
    # field instead of having to string-match `error`.
    return Frame(
        kind=FrameKind.SYSTEM,
        source=_coerce_str(raw.get("source")) or envelope_type,
        msg=msg,
        error=f"unrecognised envelope type: {envelope_type!r}",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(
    raw: "str | bytes", option_readers: Sequence[OptionReader] = (),
    attachment_readers: Sequence[AttachmentReader] = (),
) -> Frame:
    """Parse one raw WebSocket message into a normalised ``Frame``.

    This function NEVER raises. All errors are captured as
    ``Frame(kind=FrameKind.ERROR, error=<reason>)`` -- an exception here would be
    raised on the reader thread and kill the socket.
    """
    # Step 1 -- JSON decode
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return Frame(kind=FrameKind.ERROR, error=f"JSON decode error: {exc}")

    if not isinstance(payload, dict):
        return Frame(
            kind=FrameKind.ERROR,
            error=f"expected JSON object, got {type(payload).__name__}",
        )

    # Step 2 -- route on envelope shape
    text = payload.get("text")
    if isinstance(text, dict):
        # Primary shape: {"text": {"msg", "source", ...}}
        try:
            return _parse_text_envelope(text, option_readers, attachment_readers)
        except Exception as exc:  # noqa: BLE001
            return Frame(kind=FrameKind.ERROR, error=f"text envelope parse error: {exc}")

    if isinstance(text, str):
        # Degenerate shape: {"text": "<raw string>"} -- treat as a bot chunk
        finish_reason = _coerce_str(payload.get("finish_reason")) or None
        return Frame(
            kind=FrameKind.BOT_FINAL if finish_reason else FrameKind.BOT_CHUNK,
            source="bot",
            msg=text,
            finish_reason=finish_reason,
        )

    # Step 3 -- legacy envelope (type field present, no text dict)
    if "type" in payload or any(k in payload for k in ("message", "msg", "content")):
        try:
            return _parse_legacy_envelope(payload, option_readers, attachment_readers)
        except Exception as exc:  # noqa: BLE001
            return Frame(kind=FrameKind.ERROR, error=f"legacy envelope parse error: {exc}")

    return Frame(kind=FrameKind.ERROR, error="unrecognised frame shape -- no 'text' key")
