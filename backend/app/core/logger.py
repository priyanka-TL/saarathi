import logging
import json
from app.core.context import request_id_var
from app.core.settings import settings as config

class RequestIDFilter(logging.Filter):
    """Injects the current request id into the log record if available.

    Reads the ContextVar set by RequestIDMiddleware. Outside a request (startup,
    the Mitra reader/reaper threads, CLI scripts) it resolves to None, exactly
    as the previous `has_request_context()` guard did.
    """
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True

class JSONFormatter(logging.Formatter):
    """Formats log records as structured JSON, ensuring no message content is logged directly."""
    def format(self, record):
        # Build the structured log dictionary
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None)
        }
        
        # Include any extra attributes explicitly set on the log record
        # (ignoring standard logging attributes to avoid clutter)
        standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "taskName",
            "request_id", "message"
        }
        
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                log_record[key] = value

        return json.dumps(log_record)

def get_logger(name: str) -> logging.Logger:
    """
    Creates and configures a logger for the given module name.
    """
    logger = logging.getLogger(name)
    
    # Only configure if it doesn't already have handlers to avoid duplicate logs
    if not logger.handlers:
        level = getattr(logging, config.LOG_LEVEL, logging.INFO)
        logger.setLevel(level)
        
        # Add the request ID filter
        logger.addFilter(RequestIDFilter())
        
        formatter = JSONFormatter(datefmt='%Y-%m-%dT%H:%M:%SZ')
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        logger.addHandler(console_handler)
        
    return logger
