"""The ONE wording for a call to another service that failed.

Every exception message, 503 detail, and log line reservations writes about a
failed call to inventory, cabling, execution, or auth is built from fixed text,
the service's name, the HTTP status code, and the exception's class name. Never
from what the other service answered (its body, or a `detail` it sent) and never
from an exception's own text (`str(exc)`, `repr(exc)`, its args), since an
exception raised while talking to another service can quote what that service
sent or the address it was reached at.

- `UpstreamUnavailable`: a `RuntimeError`, so the routes answer it as 503 with
  its message. Every message is fixed text or one this module builds.
- `failure_label(exc)`: `HTTP <status>` for an `httpx.HTTPStatusError`, else the
  exception's class name.
- `unreachable(service, exc)`: `Failed to contact <service> service: <label>`.
- `answered(call, status_code)`: `<call> returned <status_code>`.
- `unavailable_detail(exc)`: the 503 detail a route answers for a
  `RuntimeError`: the message of an `UpstreamUnavailable`, and for any other
  `RuntimeError` the fixed `SERVICE_UNAVAILABLE`, logged at ERROR by class name
  and the innermost frame of its traceback.
- `traceback_for(exc)`: the `exc_info` argument of a failure line about a call
  to another service: no traceback when the error or any exception it chains is
  an `httpx.HTTPStatusError`, else the error itself.
"""

import logging
import traceback

import httpx

logger = logging.getLogger(__name__)

# The 503 detail for a RuntimeError this module did not word.
SERVICE_UNAVAILABLE = "A service this request depends on is unavailable"


class UpstreamUnavailable(RuntimeError):
    """A call to another service failed. The message is fixed text, or one built by
    `unreachable` or `answered`, never text the other service or an exception held."""


def failure_label(exc: BaseException) -> str:
    """`HTTP <status>` for an HTTP status error, else the exception's class name."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _chain(exc: BaseException):
    """`exc` and every exception a traceback of it prints: its `__cause__`, its
    `__context__`, and the members of an exception group, each once."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


def traceback_for(exc: BaseException) -> BaseException | None:
    """The `exc_info` argument of a log line about a failed call to another service.

    None (no traceback) when `exc` or any exception it chains is an
    `httpx.HTTPStatusError`: that error's text is the status line the other service
    sent, reason phrase included, and the request URL, userinfo included. The line
    names the status through `failure_label` instead. Any other error keeps its
    traceback: for a transport error that holds the httpx or OS error text, which
    for a protocol error includes one line of what the other side sent.
    """
    if any(isinstance(linked, httpx.HTTPStatusError) for linked in _chain(exc)):
        return None
    return exc


def unreachable(service: str, exc: BaseException) -> UpstreamUnavailable:
    """The failure of a call that raised: `Failed to contact <service> service: <label>`."""
    return UpstreamUnavailable(f"Failed to contact {service} service: {failure_label(exc)}")


def answered(call: str, status_code: int) -> UpstreamUnavailable:
    """The failure of a call that answered an error status: `<call> returned <status>`."""
    return UpstreamUnavailable(f"{call} returned {status_code}")


def _innermost_frame(exc: BaseException) -> str:
    """`<file>:<line>:<function>` of the frame that raised `exc`, read from its
    traceback's frames only (never its message), or `no traceback`."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "no traceback"
    frame = frames[-1]
    return f"{frame.filename}:{frame.lineno}:{frame.name}"


def unavailable_detail(exc: RuntimeError) -> str:
    """The 503 detail for a RuntimeError a route caught.

    An `UpstreamUnavailable` (and `UpstreamAnswerUnusable`, its subclass) carries
    its own fixed message. Any other RuntimeError (a library's, or a programming
    error such as `NotImplementedError` or `RecursionError`) answers
    `SERVICE_UNAVAILABLE` and is logged at ERROR with its class name and the
    innermost frame of its traceback, never its message or the traceback itself.
    """
    if isinstance(exc, UpstreamUnavailable):
        return str(exc)
    location = _innermost_frame(exc)
    logger.error(
        "A request failed on an unexpected %s at %s; answered 503 with fixed wording",
        type(exc).__name__,
        location,
        extra={
            "action": "unexpected_runtime_error",
            "error_class": type(exc).__name__,
            "error_location": location,
        },
    )
    return SERVICE_UNAVAILABLE
