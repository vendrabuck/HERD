"""Structured JSON logging for HERD services."""

import json
import logging
import re
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Computed once, from a fresh LogRecord, rather than hand-written: this is
# every attribute name Python's logging module puts on a LogRecord itself, so
# it tracks stdlib additions automatically (Python 3.12 added "taskName"; a
# hardcoded list would have missed it). "message" and "asctime" are added by
# logging.Formatter.format() rather than by LogRecord.__init__, so they are
# not in __dict__ yet at this point and are added by hand.
RESERVED_LOG_RECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime"}

# Top-level keys this formatter's own envelope owns. An extra whose name
# collides with one of these is renamed to extra_<name> instead of
# overwriting the envelope value; see JSONFormatter.format.
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"timestamp", "level", "service", "logger", "message", "exception"}
)

# Key-name redaction (issue #872, shape 3): any extra whose key matches this
# pattern (case-insensitive substring) has its value replaced rather than
# serialized. This is a heuristic, key-name-only backstop, not a guarantee:
# a secret value logged under a neutral key such as "error" is not caught,
# and per the #840 decision a driver's raw exception text is already allowed
# to appear in a log MESSAGE (as opposed to an extra's value), which this
# pattern does not and cannot police.
#
# The "token" branch carries its own exclusions because a plain substring
# match on "token" also catches non-secret keys already in use for token
# *counting*: ai_client's input_tokens/output_tokens metering and an
# id-referencing token_id (the database row id of an API token, not the
# token value). Those are excluded by name shape rather than by an
# allowlist, so a new counting key such as token_count stays excluded too.
_REDACT_KEY_PATTERN = re.compile(
    r"password|passwd|secret|authorization|api[-_]?key|kek|cookie|credential"
    r"|(?<!input_)(?<!output_)token(?!_id)(?!_count)(?!s(?:_|$))",
    re.IGNORECASE,
)

_REDACTED_VALUE = "[redacted]"
_UNSERIALIZABLE_VALUE = "<unserializable>"


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON.

    Every LogRecord attribute that is not one of Python logging's own
    reserved attributes (RESERVED_LOG_RECORD_ATTRS, computed once at import
    time from a fresh LogRecord) is emitted as a top-level JSON key, instead
    of the fixed eleven-key allowlist this formerly used (issue #872): most
    structured `extra=` context was silently dropped. The eleven
    formerly-allowlisted keys (method, path, status_code, duration_ms,
    user_id, action, email, username, role, device_id, reservation_id) still
    serialize exactly as before, and a None-valued extra is still omitted.

    Envelope protection: timestamp, level, service, logger, message, and
    exception are this formatter's own envelope keys and are never
    overwritten by an extra; a colliding extra name is emitted instead as
    extra_<name>. In practice "message" can never collide, because it is
    itself a reserved LogRecord attribute name: the stdlib logging module
    refuses extra={"message": ...} at the call site, before this formatter
    ever runs.

    Redaction: an extra whose KEY looks like it names a credential is
    redacted to "[redacted]" before serialization; see _REDACT_KEY_PATTERN
    for the exact rule and its caveats.

    Serialization never raises because of a bad extra: values are serialized
    with json.dumps(..., default=str) as before, and if a value defeats even
    that (for example an object whose __str__ itself raises), only that
    key's value is replaced with a placeholder rather than losing the whole
    log line.
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        log: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.default_time_format),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in RESERVED_LOG_RECORD_ATTRS or value is None:
                continue
            out_key = f"extra_{key}" if key in ENVELOPE_KEYS else key
            log[out_key] = _REDACTED_VALUE if _REDACT_KEY_PATTERN.search(key) else value

        if record.exc_info and record.exc_info[1]:
            log["exception"] = self.formatException(record.exc_info)

        try:
            return json.dumps(log, default=str)
        except Exception:
            # A value's __str__ raised, defeating even default=str. Isolate
            # and replace the offending key(s) instead of losing the whole
            # log line; the envelope keys built above are plain strings and
            # are never the cause, so they are skipped.
            for key in log:
                if key in ("timestamp", "level", "service", "logger", "message", "exception"):
                    continue
                try:
                    json.dumps(log[key], default=str)
                except Exception:
                    log[key] = _UNSERIALIZABLE_VALUE
            return json.dumps(log, default=str)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs method, path, status code, and duration for every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        logger = logging.getLogger("herd.access")
        logger.info(
            "%s %s %d %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


def setup_logging(service_name: str, level: str = "INFO") -> None:
    """Configure root logger with JSON handler; suppress noisy loggers."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter(service_name))

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    # Suppress noisy loggers
    for name in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
