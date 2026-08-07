"""Structured JSON logging, configured ONCE on the root logger.

WHY THE ROOT LOGGER AND NOT PER-MODULE
--------------------------------------
`get_logger(name)` used to attach a `JSONFormatter` handler and a
`RequestIDFilter` to each logger it was asked for. That worked only for modules
that went through it: the nine that used a plain `logging.getLogger(__name__)`
-- the voice router, the tool-execution repository, all three Bhashini modules
and all four storage modules -- got neither, so their output was unformatted
text with no `request_id` at all. Half the app was invisible to log search, and
which half depended on an import style nothing enforced.

Configuring the root logger instead means every logger in the process inherits
the formatter by propagation, however it was obtained. `get_logger` is now a
thin passthrough kept for the existing call sites.

THE FILTER GOES ON THE HANDLER, NOT THE LOGGER
----------------------------------------------
This is the subtle part, and getting it wrong reintroduces exactly the bug this
module exists to fix. `logging` runs a LOGGER's filters only for records logged
directly to that logger -- they are NOT consulted for records propagated up from
a child. A `RequestIDFilter` on the root logger would therefore fire for
`logging.getLogger()` and for nothing else. A filter on the root HANDLER runs
for every record that reaches it, which is all of them.
"""
import json
import logging
from typing import Optional

from app.core.context import request_id_var

#: Attributes `logging` puts on every record. Anything NOT in here arrived via
#: `extra=` and is a field the caller wanted in the output.
_STANDARD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "request_id", "message",
})


class RequestIDFilter(logging.Filter):
    """Stamps the current request id onto every record.

    Reads the ContextVar set by RequestIDMiddleware. Outside a request (startup,
    the Mitra reader/reaper threads, CLI scripts) it resolves to None, exactly
    as the previous `has_request_context()` guard did.

    MUST be installed on a HANDLER. See the module docstring.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line: the standard envelope plus any `extra=` fields.

    Anything passed as `extra={...}` is promoted to a top-level key rather than
    being interpolated into the message, which is what makes a log searchable by
    conversation, tenant, agent, provider, model or latency.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                log_record[key] = value

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger. Call once, from create_app().

    Idempotent: a second call replaces the handler rather than adding another,
    so an accidental double-invocation cannot double every log line. Anything
    already attached to root (uvicorn's default handler, a stray
    `logging.basicConfig`) is cleared for the same reason.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))

    for existing in root.handlers[:]:
        root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%SZ"))
    handler.addFilter(RequestIDFilter())
    root.addHandler(handler)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """A logger for `name`.

    A plain passthrough. Formatting and the request id come from the root
    handler that `configure_logging` installed, so `logging.getLogger(__name__)`
    is equally correct and the two styles can coexist -- which they do.
    """
    return logging.getLogger(name)
