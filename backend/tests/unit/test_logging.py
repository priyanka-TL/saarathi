"""The logging contract: every module gets JSON, and every record gets a request id.

These exist because the previous setup silently applied to only half the app.
`get_logger(name)` attached a formatter and a filter per-logger, so the nine
modules that used a plain `logging.getLogger(__name__)` -- the voice router, the
tool-execution repository, all three Bhashini modules, all four storage modules
-- emitted unformatted text with no `request_id`. Nothing failed; the lines just
were not searchable, and which modules were affected depended on an import style
no test checked.

The first test below is the one that matters: it logs through a bare
`logging.getLogger()`, deliberately NOT through `get_logger`, because that is
the path that used to be broken.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core.context import request_id_var
from app.core.logger import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Put the root logger back exactly as it was.

    configure_logging() clears root's handlers by design, and pytest's own
    capture plugin lives there.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _emit(logger: logging.Logger, capsys, **kwargs) -> dict:
    """Log one line and return it parsed."""
    logger.warning("hello %s", "world", **kwargs)
    return json.loads(capsys.readouterr().err.strip())


def test_a_module_using_plain_getLogger_still_gets_json_and_a_request_id(capsys):
    """THE REGRESSION TEST. A logger obtained without get_logger must still be
    formatted and still carry the request id -- that is the whole point of
    configuring the root handler rather than each logger."""
    configure_logging("INFO")
    token = request_id_var.set("req-abc123")
    try:
        record = _emit(logging.getLogger("app.integrations.storage.aws"), capsys)
    finally:
        request_id_var.reset(token)

    assert record["message"] == "hello world"
    assert record["level"] == "WARNING"
    assert record["logger"] == "app.integrations.storage.aws"
    assert record["request_id"] == "req-abc123"


def test_get_logger_and_plain_getLogger_produce_identical_envelopes(capsys):
    """The two import styles coexist in this codebase. Neither may be better."""
    configure_logging("INFO")

    via_helper = _emit(get_logger("app.services.x"), capsys)
    via_plain = _emit(logging.getLogger("app.services.x"), capsys)

    assert via_helper.keys() == via_plain.keys()
    assert via_helper["message"] == via_plain["message"]


def test_extra_fields_are_promoted_to_top_level_keys(capsys):
    """`extra=` is what makes a log queryable by conversation/agent/latency
    instead of requiring a substring search of the message."""
    configure_logging("INFO")
    record = _emit(
        logging.getLogger("app.services.orchestration"),
        capsys,
        extra={"conversation_id": "c-1", "agent": "saarthi", "latency_ms": 42},
    )

    assert record["conversation_id"] == "c-1"
    assert record["agent"] == "saarthi"
    assert record["latency_ms"] == 42


def test_request_id_is_null_outside_a_request(capsys):
    """Startup and the Mitra background threads log with no request context.
    That must be a null field, not a crash and not a missing key."""
    configure_logging("INFO")
    record = _emit(logging.getLogger("app.core.bootstrap"), capsys)

    assert record["request_id"] is None


def test_configure_logging_twice_does_not_double_each_line(capsys):
    """Idempotence. Two handlers on root would emit every line twice, which is
    the classic failure mode of moving logging config to the root logger."""
    configure_logging("INFO")
    configure_logging("INFO")

    logging.getLogger("app.whatever").warning("once")
    lines = [ln for ln in capsys.readouterr().err.strip().splitlines() if ln]

    assert len(lines) == 1


def test_the_level_comes_from_the_argument(capsys):
    """LOG_LEVEL=WARNING must actually suppress info lines."""
    configure_logging("WARNING")

    logging.getLogger("app.whatever").info("should not appear")
    assert capsys.readouterr().err.strip() == ""


def test_an_exception_is_serialised_rather_than_lost(capsys):
    """logger.exception() inside an except block must put the traceback in the
    JSON, not print it separately where it would not be searchable."""
    configure_logging("INFO")

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("app.whatever").exception("failed")

    record = json.loads(capsys.readouterr().err.strip())
    assert "ValueError: boom" in record["exception"]
