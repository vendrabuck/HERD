"""The ONE request-validation (422) handler every HERD service installs (issue #1455).

FastAPI's default handler renders each pydantic error with its `input` and `ctx`.
`input` is the submitted value, and for a missing field it is the whole body: a
secrets create without `name` echoed the plaintext `data`, a login echoed the
password, a webhook registration its signing secret, a device create the values of
its `field_data`. The default handler also fails on a lone surrogate: it echoes the
input back, Starlette cannot encode a surrogate as UTF-8, and the 422 became a 500.

This handler renders each error as exactly `{type, loc, msg}` and drops every other
key (`input`, `ctx`, `url`); `install_validation_errors` also makes the app's OpenAPI
document advertise that shape. What that guarantees, exactly:

- No 422 carries `input` or `ctx`, so none holds a submitted value as such: no
  secret's `data` value, password, webhook signing secret, or `field_data` value.
- No error this handler renders becomes a 500 because of a lone surrogate: every lone
  surrogate in a string in `loc` or `msg` is replaced with U+FFFD, so the body always
  encodes. A constrained string (a length bound, a pattern) refuses a surrogate as
  `string_unicode` with a fixed message; a plain `str`, and the keys and values of a
  `dict[str, str]`, accept one. A key of a free-form mapping can therefore carry one
  into `loc`, which is why the replacement is needed.
- Rule for validator authors: a custom validator's message must never hold a
  submitted string as is. Pydantic-core cannot build an error whose message holds a
  lone surrogate: a `ValueError` or `AssertionError` raised with one becomes a
  UnicodeEncodeError instead of a validation error (a `PydanticCustomError` fails
  the same way when its errors are read), so this handler never runs and the route
  answers 500. Quote a submitted value with `repr` (`{value!r}`), or a list of them
  (whose `str` is the `repr` of each item), which writes a surrogate, and any control
  character, as its escape. `services/common/tests/test_validation_errors.py` pins
  both halves.
- It is NOT a promise that no submitted text appears at all. Two keys still can:
  - `loc` names the keys the caller sent where a value failed or a key was refused:
    an unknown key on an `extra="forbid"` body, and a key of a free-form mapping.
    For the secrets service that is a key inside `data`
    (`["body", "data", "password"]` when that value is not a string), never the
    value; a caller who puts secret material in a key name gets it back.
  - `msg` is the validator's own text. Pydantic's built-in messages name the
    permitted values of an enum or literal, never the input, but a few quote part
    of it: the character at fault in a UUID, an email's invalid character or its
    IP-literal domain, the tag of a discriminated union. Three of the services' own
    validators quote input, each with `repr`, none of them a secret-bearing field:
    inventory's dropdown default and options
    (`FieldDefinition.validate_options_and_default`), integration's unknown event
    types (`WebhookCreate._validate_events`), and user-profile's `page_sizes` key
    names (`_validate_page_sizes`).
- A body a route builds itself (`HTTPException(detail=...)`) is not rendered by this
  handler, and some such details quote a submitted value.

A lone surrogate is refused at the body, for every JSON body (issues #1456, #1458,
#1459). A plain `str` field, the keys and values of a `dict[str, str]`, and any value
typed `Any` accept one, and every later step that must encode it as UTF-8 then fails: a
JSON response, an httpx call to another service, a database write, a NATS payload. So
`install_validation_errors` also installs `RefuseLoneSurrogates`. What it guarantees,
exactly:

- It looks only at a body FastAPI parses as JSON: content type `application/json` or
  an `application/*+json` subtype (FastAPI's own rule; with no content type FastAPI
  does not parse the body, `strict_content_type` being on everywhere in HERD). Any
  other body, multipart, a form, `text/plain`, octet-stream, or none, is passed through
  unread and unbuffered. A query string, a path, an urlencoded form, a multipart field,
  and a header cannot carry a lone surrogate anyway: each is percent-decoded with
  replacement, decoded strictly, or decoded as Latin-1. A JSON document a route parses
  itself out of an uploaded file is its own check, with `holds_lone_surrogate`.
- It never parses the body. Whether the body holds a lone surrogate is decided from
  its raw text (`_raw_text_holds_a_lone_surrogate`): for a body that is valid JSON the
  answer is exactly whether any member, also one a later duplicate key replaces, holds
  one once parsed. A body that is not valid JSON but holds a surrogate written as raw
  bytes, or an escape that would be one, is refused too, instead of the route's own
  JSON error; both are client errors.
- The answer is always the one item `LONE_SURROGATE_BODY_ERROR`,
  `{"type": "string_unicode", "loc": ["body"], "msg": ...}` with pydantic's own type
  and message, whatever field holds the surrogate: it never names the field, never
  holds the value, and never grows with the body. A field that pydantic itself would
  refuse (a constrained string) is answered this way too, since the check runs first.
- It runs when the app reads the body, so a route that does not exist (404), a method
  not allowed (405), and a route that takes no body are unchanged. A route that reads
  its body (every route with a body parameter) reads it before its dependencies and
  its own validation run, so the refusal comes before authentication, before a role
  gate, and before the schema, as FastAPI's own 422 for a body that is not JSON does.
- Its whole cost is that scan: linear in the body's length, at most about two copies
  of the body in memory, nothing that grows with the number of elements or escapes.
  It runs on the event loop, like FastAPI's own parse. The measured costs are in
  OPS-VALID-8 (`docs/specs/operations-and-observability.md`); they are observations,
  not a bound.

A request body is bounded in size, for every body and every service, config included
(issue #1466). Nothing else bounds one: the gateway has no body limit, uvicorn has none,
and FastAPI reads and parses a whole JSON body before a route's dependencies run, so
before authentication. So `install_validation_errors` also installs `LimitRequestBody`,
outside the surrogate check. What it guarantees, exactly:

- A route's limit is `max_body_bytes` (each service passes its
  `MAX_REQUEST_BODY_BYTES` setting), or, for a route whose endpoint is marked with
  `extends_body_limit(extra_bytes)`, `max_body_bytes + extra_bytes()`. The mark is the
  one way to raise a route's limit; it is used for three reasons only: an upload (the
  files the route itself already bounds), a route that legitimately takes a large
  JSON body (`LARGE_JSON_BODY_BYTES`: a canvas, a device configuration), and a route
  another HERD service forwards a body to (`FORWARD_ENVELOPE_BYTES`, what the
  forwarding service adds to a body it admitted, on top of `LARGE_JSON_BODY_BYTES`
  when that body came through a large-JSON route). The extra bytes add to the
  setting, so raising `MAX_REQUEST_BODY_BYTES` raises every marked route by as much.
- It judges a body when the app first reads it, after routing, so it knows the route.
  A request whose `Content-Length` declares more than the limit is refused at that
  first read, before one byte of the body is read. Otherwise each `http.request`
  message is counted as it is handed on, and the read that takes the count past the
  limit is refused instead of handed on: a chunked body, or one longer than its
  `Content-Length` said, never reaches the app past the limit. A body of exactly the
  limit is accepted. A `Content-Length` that is not a whole number is ignored (an HTTP
  server refuses one before the app runs); the count still bounds that body. Several
  `Content-Length` headers are each judged; any length of digits is compared exactly.
- The refusal is 413 with the one item `body_too_large_error(limit)`,
  `{"type": "body_too_large", "loc": ["body"], "msg": "Request body is larger than
  <limit> bytes"}`. It names the limit, never the body, and never grows with it. It
  does not close the connection: closing with the client still sending makes the
  kernel reset the connection, and a client that is still writing (httpx, the
  gateway) then sees the reset instead of the 413. What is left of the body is read
  and discarded by the HTTP server (uvicorn drops body data that arrives after the
  response is complete, in both its h11 and httptools protocols), so memory and parse
  work stay bounded, but the bytes still cross the network: this check does not bound
  bandwidth. One INFO log line names the limit and whether the body was declared or
  received over it; never any of its content.
- It refuses when the body is read. An unknown route, a method not allowed, and a
  route that never reads its body and answers without streaming are unchanged
  (nothing of the body is read, so nothing is buffered). A route that STREAMS its
  answer (a `StreamingResponse`) without reading its body does not, a known limit
  (OPS-VALID-9, issue #1483): the response's disconnect listener reads the body while
  the stream runs, and behind the request logger, which holds the response's start, a
  body whose `Content-Length` is over the limit ends in `RuntimeError: No response
  returned.` (a 500, or a reset) instead of the stream, and a chunked one over it cuts
  the stream short; nothing of the body reaches the route either way. So a streaming
  route takes its body as a parameter, and
  `tests/unit/test_request_body_limit_wiring.py` fails on one that does not unless it
  is listed there by decision (none is). Every route with a body (JSON or a form)
  reads it before its dependencies run, so the refusal comes before authentication,
  as the surrogate check's does; and the surrogate check reads through this one, so a
  body over the limit is refused before it is scanned, and the scan never holds more
  than the limit.
- Nothing is refused once the response has started, since the answer cannot change by
  then. Starlette's disconnect listener (a streaming response to a route that never
  read its body) can make the first read while the response is being started
  concurrently, so before raising a refusal the check yields to the event loop once
  and looks again; if the response started meanwhile, or had started before, the read
  that would be refused, or that takes the count past the limit, answers
  `http.disconnect` instead of handing the bytes on, which the listener takes as the
  client leaving.
- Its cost is constant per message and per request: no copy of the body, no second
  pass, one scan of the headers at the first read. A websocket, the lifespan, and a
  request whose body is never read pass through with nothing judged.

Every service installs it from its `app/main.py`; `tests/unit/test_validation_errors_wiring.py`
fails when one does not. The config service, which must not import herd_common,
installs a standalone copy (`services/config/app/validation_errors.py`) whose code is
pinned identical to this module's by `services/config/tests/test_validation_errors.py`,
and installs it with `refuse_lone_surrogates=False`: config already judges a lone
surrogate in each password with an answer of its own (OPS-CONFIG-8), which the body
check would replace. The wiring guard allows that keyword to config alone. Config
installs the body limit like every other service.
First written for auth (issue #1426), copied for config (issue #1428).
"""

from __future__ import annotations

import email.message
import json
import logging
import re
from collections.abc import Callable
from typing import Any, TypeVar

import anyio.lowlevel
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware

logger = logging.getLogger(__name__)

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _encodable(value: Any) -> Any:
    """`value` with every lone surrogate in a string replaced by U+FFFD."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    return value


def render_validation_errors(errors: Any) -> list[dict[str, Any]]:
    """Each error as `{type, loc, msg}`, encodable as UTF-8, with no input value."""
    return [
        {
            "type": _encodable(error.get("type")),
            "loc": [_encodable(part) for part in error.get("loc", ())],
            "msg": _encodable(error.get("msg")),
        }
        for error in errors
    ]


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": render_validation_errors(exc.errors())},
    )


# The keys every rendered error has, and the only ones the OpenAPI document lists.
RENDERED_KEYS = ("type", "loc", "msg")


def restrict_validation_error_schema(openapi: dict[str, Any]) -> dict[str, Any]:
    """Make the generated `ValidationError` component list only `RENDERED_KEYS`.

    FastAPI's component also lists `input` and `ctx`, which this handler never
    sends. Changed in place and returned; a document without the component (no
    route takes a body or a parameter) is returned unchanged.
    """
    component = openapi.get("components", {}).get("schemas", {}).get("ValidationError")
    if component is not None:
        properties = component.get("properties", {})
        component["properties"] = {k: v for k, v in properties.items() if k in RENDERED_KEYS}
    return openapi


# Pydantic's own type and message for a constrained string holding a lone surrogate.
# The body check's one item reads the same, at `["body"]`.
STRING_UNICODE_TYPE = "string_unicode"
STRING_UNICODE_MSG = "Input should be a valid string, unable to parse raw data as a unicode string"
LONE_SURROGATE_BODY_ERROR = {
    "type": STRING_UNICODE_TYPE,
    "loc": ["body"],
    "msg": STRING_UNICODE_MSG,
}

# The bytes a UTF-8 JSON text needs to hold a lone surrogate: the start of a `\u`
# escape, or the lead byte 0xED of a surrogate written as raw UTF-8 bytes (`json.loads`
# decodes with `surrogatepass`). A body with neither is passed after two searches.
_RAW_SURROGATE_BYTES = re.compile(rb"\xed[\xa0-\xbf]")

# The JSON escape grammar, for the raw scan. In a JSON string every backslash starts an
# escape, so once each escaped backslash pair is replaced by one other byte, every
# backslash left starts an escape. `json.loads` joins a high-surrogate escape
# (`\uD800` to `\uDBFF`) immediately followed by a low-surrogate escape (`\uDC00` to
# `\uDFFF`) into one code point; every other surrogate escape stays a lone surrogate.
# On that copy, lower-cased (a hex digit may be either case): a high escape not
# followed by a low one is lone (a search whose literal prefix `\ud` lets the engine
# skip to candidates); with none, every high escape begins a pair, which holds one low,
# so a low escape is lone exactly when there are more low escapes than high ones.
_HIGH_NOT_PAIRED = re.compile(rb"\\ud[89ab][0-9a-f]{2}(?!\\ud[c-f][0-9a-f]{2})")
_LOW_ESCAPES = (b"\\udc", b"\\udd", b"\\ude", b"\\udf")
_HIGH_ESCAPES = (b"\\ud8", b"\\ud9", b"\\uda", b"\\udb")


def _escapes_hold_a_lone_surrogate(text: bytes) -> bool:
    """Whether the UTF-8 JSON text `text` holds a lone surrogate escape.

    Makes at most two copies of the text (the escaped backslash pairs replaced, and
    lower-cased), each in C; the search and the eight counts are C passes too. So the
    cost is linear in the text's length, and memory is at most about twice the UTF-8
    text, whatever its number of escapes or elements (a UTF-16 or UTF-32 body is
    decoded first, which adds its own copies).
    """
    if b"\\\\" in text:
        text = text.replace(b"\\\\", b"_")
    text = text.lower()
    if _HIGH_NOT_PAIRED.search(text):
        return True
    lows = sum(text.count(low) for low in _LOW_ESCAPES)
    return lows > 0 and lows > sum(text.count(high) for high in _HIGH_ESCAPES)


def _raw_text_holds_a_lone_surrogate(body: bytes) -> bool:
    """Whether a JSON body holds a lone surrogate, decided from its raw text.

    For a body that is valid JSON the answer is exactly whether any member, also one a
    later duplicate key replaces, holds a lone surrogate once parsed. For a body that
    is not valid JSON it is whether its text holds a surrogate written as raw UTF-8
    bytes or a `\\u` escape that would be one.
    """
    encoding = json.detect_encoding(body)
    if encoding in ("utf-8", "utf-8-sig"):
        if b"\\u" not in body and b"\xed" not in body:
            return False
        if _RAW_SURROGATE_BYTES.search(body):
            return True
        return _escapes_hold_a_lone_surrogate(body)
    try:  # UTF-16 or UTF-32, decoded exactly as `json.loads` decodes it
        text = body.decode(encoding, "surrogatepass")
    except UnicodeDecodeError:
        return False
    if _LONE_SURROGATE.search(text):
        return True
    # No lone surrogate is left in the text, so it encodes; its escapes are then read
    # as a UTF-8 body's are.
    return _escapes_hold_a_lone_surrogate(text.encode("utf-8"))


def holds_lone_surrogate(value: Any) -> bool:
    """Whether a parsed JSON value holds a lone surrogate in any string or key.

    Encodes the value once in C (`json.dumps` with `ensure_ascii=False`, then UTF-8,
    which refuses a lone surrogate). A value too deep to serialize, or one that is not
    JSON data, is walked instead, with a stack, until the first string that holds one.
    """
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    except (RecursionError, TypeError, ValueError):
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                if _LONE_SURROGATE.search(item):
                    return True
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return False


def _parses_as_json(scope: Any) -> bool:
    """Whether FastAPI would parse this request's body as JSON: its content type is
    `application/json` or an `application/*+json` subtype. FastAPI (`strict_content_type`,
    on by default and turned off nowhere in HERD) does not parse a body with no
    content type, so neither does this check. The same header parsing as FastAPI's."""
    for name, value in scope.get("headers", ()):
        if name == b"content-type":
            message = email.message.Message()
            message["content-type"] = value.decode("latin-1")
            if message.get_content_maintype() != "application":
                return False
            subtype = message.get_content_subtype()
            return subtype == "json" or subtype.endswith("+json")
    return False


class RefuseLoneSurrogates:
    """ASGI middleware: a JSON request body holding a lone surrogate is a 422.

    A body FastAPI would not parse as JSON (`_parses_as_json`) is passed through unread
    and unbuffered. For a JSON body it wraps `receive`: the first time the app reads
    the body, the whole body is read, scanned with `_raw_text_holds_a_lone_surrogate`
    (no parse), and handed on as one message; when the scan finds a lone surrogate, the
    read raises `HTTPException(422)` with the one item `LONE_SURROGATE_BODY_ERROR`,
    which FastAPI's body reader passes on and the app's exception handling renders as
    `{"detail": [...]}`. A read made after the response has started (Starlette's
    disconnect listener on a route that never read its body) is handed on unchecked.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not _parses_as_json(scope):
            await self.app(scope, receive, send)
            return
        read = False
        started = False

        async def send_tracking_start(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def receive_checked() -> Any:
            nonlocal read
            if read:
                return await receive()
            read = True
            chunks = []
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return message
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            if not started and _raw_text_holds_a_lone_surrogate(body):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=[dict(LONE_SURROGATE_BODY_ERROR)],
                )
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, receive_checked, send_tracking_start)


# The default of every service's MAX_REQUEST_BODY_BYTES setting (issues #1466, #1484):
# what every route takes, including every route a caller with no token can reach, so it
# is the parse a client can cost a service before authentication. The largest body an
# unmarked route takes is a pathfinding batch at its 2,000 pairs (about 236 KB as
# compact JSON); the routes that legitimately take more are marked with
# `LARGE_JSON_BODY_BYTES`. Changing the default is this one line; the copy test and
# the compose wiring tests make the other places follow.
DEFAULT_MAX_REQUEST_BODY_BYTES = 256 * 1024

# What a route that legitimately takes a large JSON body adds to MAX_REQUEST_BODY_BYTES
# (issue #1484), marked with `extends_body_limit(lambda: LARGE_JSON_BODY_BYTES)`: a
# topology canvas (as #1466 measured it, about 1.3 KB a device and 0.5 KB a link, so
# 1,000 devices and 4,000 links come to about 3.1 MiB), a device configuration, a
# commit, and a bulk connection create. Like every mark it adds to the setting, so an
# operator who raises MAX_REQUEST_BODY_BYTES raises these routes by as much.
LARGE_JSON_BODY_BYTES = 4 * 1024 * 1024

# The smallest MAX_REQUEST_BODY_BYTES a service accepts: every body one HERD service
# sends another that carries neither a request body nor a stored document (lists of
# at most 500 ids, a 2,000-character purpose) is smaller than this, so no setting can
# make those internal calls fail (the forwarding table in
# tests/unit/test_request_body_limit_wiring.py states each one's bound).
MIN_MAX_REQUEST_BODY_BYTES = 64 * 1024

# What a HERD service adds to a body it admitted, or to a stored document, when it
# forwards it to another service; the receiving route is marked with
# `extends_body_limit(lambda: FORWARD_ENVELOPE_BYTES)`, or, when what it receives was
# admitted by a large-JSON route, with `LARGE_JSON_BODY_BYTES + FORWARD_ENVELOPE_BYTES`.
# The largest addition is the notifications preference merge (the stored preferences,
# at most a 64 KiB blob in user-profile, plus the update); the others add at most about
# 10 KB (250 device ids on a fork save).
FORWARD_ENVELOPE_BYTES = 256 * 1024

BODY_TOO_LARGE_TYPE = "body_too_large"

# The attribute `extends_body_limit` sets on a route's endpoint.
EXTRA_BODY_BYTES_ATTRIBUTE = "herd_extra_body_bytes"

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])


def body_too_large_error(limit: int) -> dict[str, Any]:
    """The one item of the 413: it names the limit, never anything of the body."""
    return {
        "type": BODY_TOO_LARGE_TYPE,
        "loc": ["body"],
        "msg": f"Request body is larger than {limit} bytes",
    }


def max_body_bytes_from_text(raw: str) -> int | None:
    """`MAX_REQUEST_BODY_BYTES` read from text the one way both readers use: ASCII
    decimal digits, surrounding whitespace ignored, at least
    `MIN_MAX_REQUEST_BODY_BYTES`. Anything else (a sign, a decimal point, an exponent,
    an underscore, a value below the floor) is None."""
    digits = raw.strip()
    if not (digits.isascii() and digits.isdigit()):
        return None
    value = int(digits)
    return value if value >= MIN_MAX_REQUEST_BODY_BYTES else None


def extends_body_limit(extra_bytes: Callable[[], int]) -> Callable[[_Endpoint], _Endpoint]:
    """Mark a route endpoint whose body may hold `extra_bytes()` more than the default
    limit: an upload route (the files it bounds itself), a route that legitimately
    takes a large JSON body (`LARGE_JSON_BODY_BYTES`), or a route another HERD
    service forwards a body to (`FORWARD_ENVELOPE_BYTES`, added to
    `LARGE_JSON_BODY_BYTES` when what it forwards came through a large-JSON route).
    `extra_bytes` is called at each request, so it reads the bound the route enforces
    (a setting) rather than a copy of it. Place it under the route decorator; the
    endpoint is returned unchanged."""

    def mark(endpoint: _Endpoint) -> _Endpoint:
        setattr(endpoint, EXTRA_BODY_BYTES_ATTRIBUTE, extra_bytes)
        return endpoint

    return mark


def route_body_limit(route: Any, max_body_bytes: int) -> int:
    """The body limit of a route: `max_body_bytes`, plus its extra bytes when its
    endpoint is marked with `extends_body_limit`. Any other route (none matched yet, a
    route with no endpoint) takes `max_body_bytes`."""
    extra_bytes = getattr(getattr(route, "endpoint", None), EXTRA_BODY_BYTES_ATTRIBUTE, None)
    if extra_bytes is None:
        return max_body_bytes
    return max_body_bytes + extra_bytes()


def declares_more_than(headers: Any, limit: int) -> bool:
    """Whether any `Content-Length` header declares more than `limit` bytes. A value
    that is not a whole number (empty, negative, text) declares nothing. The digits
    are compared by length first, so a value of any length is judged exactly and none
    longer than the limit's own digits is converted."""
    limit_digits = len(str(limit))
    for name, value in headers:
        if name == b"content-length":
            digits = value.strip()
            if not digits.isdigit():
                continue
            significant = digits.lstrip(b"0")
            if len(significant) > limit_digits or (
                len(significant) == limit_digits and int(significant) > limit
            ):
                return True
    return False


def _log_refusal(limit: int, reason: str) -> None:
    logger.info(
        "Request body refused: %s over the limit of %d bytes",
        reason,
        limit,
        extra={"action": "request_body_too_large", "limit_bytes": limit, "reason": reason},
    )


class LimitRequestBody:
    """ASGI middleware: a request body over its route's limit is a 413.

    For an HTTP request it wraps `receive`. At the first read (after routing, so
    `scope["route"]` names the route) it takes the route's limit
    (`route_body_limit`), and a `Content-Length` declaring more is refused before
    anything is read. Each `http.request` message is then counted, and the read that
    takes the count past the limit raises `HTTPException(413)` (FastAPI's body reader
    passes it on, as for the surrogate check) instead of handing the message on.
    Before raising, it yields to the event loop once: if the response has started by
    then (a streaming response whose disconnect listener made the read), or had
    started before, nothing is refused and the read answers `http.disconnect`, as
    does every later read. Messages are handed on as received, never copied or
    joined.
    """

    def __init__(self, app: Any, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = -1  # judged at the first read
        received = 0
        over = False
        started = False

        async def send_tracking_start(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def refuse(reason: str) -> Any:
            nonlocal over
            over = True
            # A read the route makes is never concurrent with a response it has not
            # returned yet; a read made by a streaming response's disconnect listener
            # is, and that response's start may be one scheduling step away.
            if not started:
                await anyio.lowlevel.checkpoint()
            if started:
                return {"type": "http.disconnect"}
            _log_refusal(limit, reason)
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=[body_too_large_error(limit)],
            )

        async def receive_limited() -> Any:
            nonlocal limit, received
            if over:
                return await refuse("received")
            if limit < 0:
                limit = route_body_limit(scope.get("route"), self.max_body_bytes)
                if declares_more_than(scope.get("headers", ()), limit):
                    return await refuse("declared")
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    return await refuse("received")
            return message

        await self.app(scope, receive_limited, send_tracking_start)


def install_validation_errors(
    app: FastAPI, *, max_body_bytes: int, refuse_lone_surrogates: bool = True
) -> None:
    """Use `validation_error_handler` for every 422 and advertise its shape; bound
    every request body with `LimitRequestBody` at `max_body_bytes` (the service's
    `MAX_REQUEST_BODY_BYTES`); and, unless `refuse_lone_surrogates` is False (the
    config service alone), refuse a request body holding a lone surrogate with
    `RefuseLoneSurrogates`, inside the limit.

    The OpenAPI override is FastAPI's documented one: replace `app.openapi`
    with a function that builds the default document once, adjusts it, and
    caches it in `app.openapi_schema`.
    """
    if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
        raise TypeError("max_body_bytes must be an int")
    if max_body_bytes < 1:
        raise ValueError("max_body_bytes must be at least 1")
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # Innermost, next to the routes, whatever the order of the app's other
    # middleware: `add_middleware` would put them outermost, and a
    # BaseHTTPMiddleware (the request logger) runs the `receive` it passes on in a
    # task group, which wraps a refusal in an ExceptionGroup that FastAPI's body
    # reader turns into a 400. The limit goes first, so the surrogate check reads
    # through it. The same guard as `add_middleware`.
    if app.middleware_stack is not None:
        raise RuntimeError("Cannot add middleware after an application has started")
    app.user_middleware.append(Middleware(LimitRequestBody, max_body_bytes=max_body_bytes))
    if refuse_lone_surrogates:
        app.user_middleware.append(Middleware(RefuseLoneSurrogates))

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = restrict_validation_error_schema(FastAPI.openapi(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
