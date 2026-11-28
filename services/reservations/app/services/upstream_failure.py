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
  `RuntimeError` the fixed `SERVICE_UNAVAILABLE`, logged by class name.
"""

import logging

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


def unreachable(service: str, exc: BaseException) -> UpstreamUnavailable:
    """The failure of a call that raised: `Failed to contact <service> service: <label>`."""
    return UpstreamUnavailable(f"Failed to contact {service} service: {failure_label(exc)}")


def answered(call: str, status_code: int) -> UpstreamUnavailable:
    """The failure of a call that answered an error status: `<call> returned <status>`."""
    return UpstreamUnavailable(f"{call} returned {status_code}")


def unavailable_detail(exc: RuntimeError) -> str:
    """The 503 detail for a RuntimeError a route caught.

    An `UpstreamUnavailable` (and `UpstreamAnswerUnusable`, its subclass) carries
    its own fixed message. Any other RuntimeError (one a library raised) answers
    `SERVICE_UNAVAILABLE`, and its class name is logged.
    """
    if isinstance(exc, UpstreamUnavailable):
        return str(exc)
    logger.warning(
        "A request failed on an unexpected %s; answered 503 with fixed wording",
        type(exc).__name__,
        extra={"action": "unexpected_runtime_error", "error_class": type(exc).__name__},
    )
    return SERVICE_UNAVAILABLE
