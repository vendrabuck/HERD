"""Structured JSON logging for HERD services."""

import json
import logging
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

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

        # Include optional structured extras
        for key in (
            "method", "path", "status_code", "duration_ms",
            "user_id", "action", "email", "username", "role",
            "device_id", "reservation_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                log[key] = value

        if record.exc_info and record.exc_info[1]:
            log["exception"] = self.formatException(record.exc_info)

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
