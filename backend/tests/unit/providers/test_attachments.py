"""Downloadable documents on a bot turn (`extra_content.download`).

Two things are under test and they are separable on purpose:

  * PARSING -- what the shared `ws_flow` parser makes of the block. It runs on
    the socket reader thread, so the governing rule is that it never raises: a
    malformed payload yields no documents, never an exception that would kill
    the connection.
  * PERMISSION -- what the provider will actually surface. A URL handed to a
    user's browser gets the same https + allowlist treatment as one we would
    fetch ourselves, but a failure DROPS the link rather than failing the turn.

The sample frame is the one captured from the live platform, verbatim.
"""
from __future__ import annotations

import json

import pytest

from app.providers.transport.frames import Attachment
from app.providers.transport.http import url_is_permitted
from app.providers.ws_flow.frames import DEFAULT_MEDIA_TYPE, parse

STATIC_HOST = "qa-mohini-static.shikshalokam.org"
PDF_URL = f"https://{STATIC_HOST}/chatbot/2/qpk/1786427418-MIP_student-focus-primary-grades.pdf"
DOCX_URL = f"https://{STATIC_HOST}/chatbot/2/qpk/1786427418-MIP_student-focus-primary-grades.docx"

PDF_MEDIA = "application/pdf"
DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def frame(extra_content=None, **over):
    """A bot frame, optionally carrying an extra_content block."""
    text = {
        "msg": "Your plan is ready to download. Is there another school "
               "challenge you'd like to work on?",
        "source": "bot",
        "finish_reason": "stop",
        "step": 1,
    }
    if extra_content is not None:
        text["extra_content"] = extra_content
    text.update(over)
    return json.dumps({"text": text})


DOWNLOAD = {
    "pdf_url": PDF_URL,
    "docx_url": DOCX_URL,
    "file_name": "MIP_student-focus-primary-grades",
}


# ---------------------------------------------------------------------------
# The captured sample
# ---------------------------------------------------------------------------

def test_the_live_sample_frame_yields_both_documents():
    parsed = parse(frame({"download": DOWNLOAD}))

    assert [(a.format, a.media_type, a.url) for a in parsed.attachments] == [
        ("docx", DOCX_MEDIA, DOCX_URL),
        ("pdf", PDF_MEDIA, PDF_URL),
    ]
    assert {a.file_name for a in parsed.attachments} == {
        "MIP_student-focus-primary-grades"
    }
    # The reply text and step are untouched by the new parsing.
    assert parsed.msg.startswith("Your plan is ready to download.")
    assert parsed.step == 1


def test_the_order_is_deterministic_not_wire_order():
    """Two formats must not swap places between turns just because the platform
    serialised its dict differently."""
    a = parse(frame({"download": {"docx_url": DOCX_URL, "pdf_url": PDF_URL}}))
    b = parse(frame({"download": {"pdf_url": PDF_URL, "docx_url": DOCX_URL}}))

    assert [x.format for x in a.attachments] == [x.format for x in b.attachments]


# ---------------------------------------------------------------------------
# One format, or none
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,fmt,media", [
    ("pdf_url", "pdf", PDF_MEDIA),
    ("docx_url", "docx", DOCX_MEDIA),
])
def test_a_single_available_format_is_not_a_special_case(key, fmt, media):
    parsed = parse(frame({"download": {key: PDF_URL, "file_name": "plan"}}))

    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].format == fmt
    assert parsed.attachments[0].media_type == media


@pytest.mark.parametrize("download", [
    {},                                              # empty block
    {"file_name": "plan"},                           # a name but no files
    {"pdf_url": None, "docx_url": None},             # both explicitly absent
    {"pdf_url": "", "docx_url": "   "},              # both blank
    {"pdf_url": 42},                                 # not a string
    {"_url": PDF_URL},                               # no format in the key
])
def test_a_block_offering_nothing_yields_nothing(download):
    """No documents means no widget -- never an empty button row, never a raise."""
    assert parse(frame({"download": download})).attachments == []


@pytest.mark.parametrize("download", ["not a dict", 42, [], None])
def test_a_malformed_download_block_is_ignored(download):
    """This runs on the reader thread: an exception here kills the socket."""
    parsed = parse(frame({"download": download}))
    assert parsed.attachments == []
    assert parsed.msg  # the reply still gets through


def test_one_missing_format_does_not_lose_the_other():
    parsed = parse(frame({"download": {"pdf_url": PDF_URL, "docx_url": None}}))
    assert [a.format for a in parsed.attachments] == ["pdf"]


# ---------------------------------------------------------------------------
# file_name
# ---------------------------------------------------------------------------

def test_file_name_is_taken_from_the_payload():
    parsed = parse(frame({"download": DOWNLOAD}))
    assert parsed.attachments[0].file_name == "MIP_student-focus-primary-grades"


def test_file_name_falls_back_to_the_url_basename():
    """Absent is normal, and a nameless download would be saved as something
    unrecognisable."""
    parsed = parse(frame({"download": {"pdf_url": PDF_URL}}))
    assert parsed.attachments[0].file_name == "1786427418-MIP_student-focus-primary-grades"


def test_the_fallback_name_carries_no_extension():
    """It is joined with the format downstream; keeping the extension here
    would produce `plan.pdf.pdf`."""
    parsed = parse(frame({"download": {"pdf_url": "https://h.test/a/plan.pdf"}}))
    assert parsed.attachments[0].file_name == "plan"


def test_the_fallback_name_survives_a_query_string_and_encoding():
    parsed = parse(frame({"download": {
        "pdf_url": "https://h.test/a/my%20plan.pdf?sig=abc&x=1",
    }}))
    assert parsed.attachments[0].file_name == "my plan"


# ---------------------------------------------------------------------------
# An unknown format still works
# ---------------------------------------------------------------------------

def test_an_unrecognised_format_is_carried_with_a_generic_media_type():
    """A platform that starts sending pptx_url needs no code change -- reading
    the format off the key is what buys that."""
    parsed = parse(frame({"download": {"pptx_url": "https://h.test/a/deck.pptx"}}))

    assert parsed.attachments[0].format == "pptx"
    assert parsed.attachments[0].media_type == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


def test_a_completely_unknown_extension_still_becomes_a_download():
    parsed = parse(frame({"download": {"zip_url": "https://h.test/a/bundle.zip"}}))
    assert parsed.attachments[0].format == "zip"
    assert parsed.attachments[0].media_type == DEFAULT_MEDIA_TYPE


# ---------------------------------------------------------------------------
# No regressions on every other frame shape
# ---------------------------------------------------------------------------

def test_a_plain_reply_is_completely_unaffected():
    parsed = parse(frame())
    assert parsed.attachments == []
    assert parsed.options == []
    assert parsed.msg.startswith("Your plan is ready")


def test_an_options_frame_still_parses_and_gains_no_documents():
    parsed = parse(frame({"options": [
        {"id": "1", "label": "Yes", "value": "yes"},
        {"id": "2", "label": "No", "value": "no"},
    ]}))

    assert [o.label for o in parsed.options] == ["Yes", "No"]
    assert parsed.attachments == []


def test_documents_and_choices_can_arrive_on_the_same_frame():
    """They are read by two independent functions, so a turn that offers a plan
    AND asks a follow-up question yields both."""
    from app.providers.saathi.frames import quick_reply_chips

    parsed = parse(
        frame({"download": DOWNLOAD, "quick_reply_chips": ["Yes", "No"]}),
        option_readers=(quick_reply_chips,),
    )

    assert [o.label for o in parsed.options] == ["Yes", "No"]
    assert len(parsed.attachments) == 2


def test_a_user_echo_never_carries_documents():
    parsed = parse(json.dumps({"text": {
        "msg": "help", "source": "user", "extra_content": {"download": DOWNLOAD},
    }}))
    assert parsed.attachments == []


def test_a_system_frame_never_carries_documents():
    parsed = parse(json.dumps({"text": {
        "msg": "boom", "source": "system", "error": "boom",
        "extra_content": {"download": DOWNLOAD},
    }}))
    assert parsed.attachments == []


def test_a_legacy_envelope_can_still_carry_documents():
    parsed = parse(json.dumps({
        "type": "message", "message": "here you go", "finish_reason": "stop",
        "extra_content": {"download": {"pdf_url": PDF_URL}},
    }))
    assert [a.format for a in parsed.attachments] == ["pdf"]


# ---------------------------------------------------------------------------
# Permission -- what will actually be surfaced
# ---------------------------------------------------------------------------

class _Conn:
    def __init__(self, allowed_hosts):
        self.allowed_hosts = tuple(allowed_hosts)


class _Provider:
    """The real filter, lifted off BaseWsFlowProvider without a socket."""

    name = "saathi"

    def __init__(self, allowed_hosts):
        self._conn = _Conn(allowed_hosts)

    _permitted_attachments = None  # bound below


def _filter(allowed_hosts, attachments):
    from app.providers.ws_flow.base import BaseWsFlowProvider

    provider = _Provider(allowed_hosts)
    return BaseWsFlowProvider._permitted_attachments(provider, attachments)


def _attachment(url, fmt="pdf"):
    return Attachment(file_name="plan", format=fmt, media_type=PDF_MEDIA, url=url)


def test_an_allowlisted_https_url_is_surfaced():
    kept = _filter([STATIC_HOST], [_attachment(PDF_URL)])
    assert [a.url for a in kept] == [PDF_URL]


def test_a_host_outside_the_allowlist_is_dropped():
    """The whole reason migration 0015 exists: the documents live on a
    different host from the API, so the allowlist has to name it."""
    kept = _filter([], [_attachment(PDF_URL)])
    assert kept == []


def test_a_lookalike_host_does_not_pass():
    kept = _filter([STATIC_HOST], [_attachment("https://evil-" + STATIC_HOST + "/x.pdf")])
    assert kept == []


def test_an_http_url_is_dropped():
    kept = _filter([STATIC_HOST], [_attachment(f"http://{STATIC_HOST}/x.pdf")])
    assert kept == []


def test_one_bad_url_does_not_take_the_good_one_with_it():
    kept = _filter(
        [STATIC_HOST],
        [_attachment("https://elsewhere.test/x.pdf"), _attachment(DOCX_URL, "docx")],
    )
    assert [a.format for a in kept] == ["docx"]


def test_dropping_logs_a_warning_naming_the_host_but_not_the_url(caplog):
    """An SSRF payload must not reach a log line -- the same rule
    ProviderSSRFError follows by omitting the URL from its message."""
    import logging

    secret_url = "https://internal.metadata.test/a/secret-path.pdf"
    with caplog.at_level(logging.WARNING):
        assert _filter([STATIC_HOST], [_attachment(secret_url)]) == []

    assert caplog.records, "a dropped download must not be silent"
    record = caplog.records[-1]

    # The host is a STRUCTURED field, not message text -- app/core/logger.py
    # emits JSON, so `extra` is what an operator actually greps.
    assert record.hosts == ["internal.metadata.test"]
    assert record.formats == ["pdf"]
    assert STATIC_HOST in record.allowed_hosts

    # ...and the path never appears anywhere, message or fields.
    everything = caplog.text + repr(record.__dict__)
    assert "secret-path" not in everything


def test_dropping_never_raises():
    """The reply text is still worth showing; one bad link must not fail a turn."""
    assert _filter([STATIC_HOST], [_attachment("not-even-a-url")]) == []


def test_the_same_rule_backs_both_policies():
    """`validate_url` (raise, before fetching) and the attachment filter (drop,
    before surfacing) must not drift apart."""
    assert url_is_permitted(PDF_URL, [STATIC_HOST]) is True
    assert url_is_permitted(PDF_URL, []) is False
