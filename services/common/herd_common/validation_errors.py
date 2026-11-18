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

A lone surrogate is refused at the body, for every route (issues #1456, #1458, #1459).
A plain `str` field, the keys and values of a `dict[str, str]`, and any value typed
`Any` accept one, and every later step that must encode it as UTF-8 then fails: a
JSON response, an httpx call to another service, a database write, a NATS payload. So
`install_validation_errors` also installs `RefuseLoneSurrogates`, which checks each
request body when the app reads it and answers 422 when the parsed JSON holds a lone
surrogate anywhere: a value, a key, nested, in a field of any type. What that
guarantees, exactly:

- The answer is the same `{"detail": [...]}` list, each item `{type, loc, msg}`, with
  pydantic's own `string_unicode` type and message, for the first
  `MAX_REPORTED_LOCATIONS` (10) locations in document order; the search stops there,
  and nothing marks that more were left out. `loc` is `["body", ...]` and ends at the
  string that holds the surrogate, or, for a key, at the object holding the key (as
  pydantic refuses such a key on an `extra="forbid"` body); it never holds the
  surrogate or the value.
- It runs only when the app reads the body, so a route that does not exist (404), a
  method not allowed (405), and a route that takes no body are unchanged. A route that
  reads its body (every route with a body parameter) reads it before its dependencies
  and its own validation run, so the refusal comes before authentication, before a
  role gate, and before the schema, as FastAPI's own 422 for a body that is not JSON
  does.
- Every body is checked except a `multipart/*` one, which is passed through unread:
  Starlette decodes a multipart field strictly as UTF-8 (falling back to Latin-1 for a
  file name), so no lone surrogate can come from one. A query string, a path, an
  urlencoded form, and a header cannot carry one either: each is percent-decoded with
  replacement or decoded as Latin-1. A JSON document a route parses itself out of an
  uploaded file is its own check, with `holds_lone_surrogate`.
- Whether a body holds one is decided from its raw text, without parsing: a surrogate
  written as raw UTF-8 bytes, a high-surrogate escape not followed by a low one, or
  more low-surrogate escapes than pairs, each run of backslashes read by its parity.
  Only a body that holds one (or one whose surrogate a later duplicate key replaces,
  or that is not JSON) is parsed, and only then are the locations searched, by a walk
  whose memory grows with the nesting depth and which stops at the tenth. So the cost
  grows with the body's length and its number of elements, never with elements times
  depth; it runs on the event loop, like FastAPI's own parse of the body. The measured
  costs are in OPS-VALID-6 (`docs/specs/operations-and-observability.md`); they are
  observations, not a bound.
- A body that is not JSON passes through, so the route answers as it would have.

Every service installs it from its `app/main.py`; `tests/unit/test_validation_errors_wiring.py`
fails when one does not. The config service, which must not import herd_common,
installs a standalone copy (`services/config/app/validation_errors.py`) whose code is
pinned identical to this module's by `services/config/tests/test_validation_errors.py`,
and installs it with `refuse_lone_surrogates=False`: config already judges a lone
surrogate in each password with an answer of its own (OPS-CONFIG-8), which the body
check would replace. The wiring guard allows that keyword to config alone.
First written for auth (issue #1426), copied for config (issue #1428).
"""

from __future__ import annotations

import json
import re
from itertools import compress
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware

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


# Pydantic's own type and message for a constrained string holding a lone surrogate,
# so the body check's refusal reads the same as the schema's.
STRING_UNICODE_TYPE = "string_unicode"
STRING_UNICODE_MSG = "Input should be a valid string, unable to parse raw data as a unicode string"

# The most locations one refusal reports. The search stops at the tenth, so a body of
# a million lone surrogates costs no more to answer than a body of ten.
MAX_REPORTED_LOCATIONS = 10

# Bytes that must be present for a UTF-8 JSON text to hold a lone surrogate: the start
# of an escape whose first hex digit is D, or the lead byte 0xED of a surrogate written
# as raw UTF-8 bytes (`json.loads` decodes with `surrogatepass`).
_MAY_HOLD_A_SURROGATE = (b"\\ud", b"\\uD", b"\xed")

# The JSON escape grammar, for the raw scan. A backslash starts an escape only when the
# run of backslashes it ends is odd. Each pattern starts at the first backslash of a run
# (a backslash not preceded by one: the literal first lets the regular-expression engine
# skip ahead to candidates), then takes the run's remaining escaped-backslash pairs
# possessively, so `\\ud800` (an escaped backslash, then text) is not an escape and
# `\\\ud800` is. `json.loads` joins a high-surrogate escape immediately followed by a
# low-surrogate escape into one code point; every other surrogate escape, and every raw
# surrogate, stays a lone surrogate in the parsed value. So a lone high is a high escape
# not followed by a low one, and a lone low exists when there are more low escapes than
# pairs (each pair holds exactly one).
_HEX = "[0-9a-fA-F]"
_HIGH = "[dD][89abAB]" + _HEX * 2
_LOW = "[dD][c-fC-F]" + _HEX * 2
_ESCAPE = r"\\(?<!\\\\)(?:\\\\)*+u"
_LONE_HIGH = _ESCAPE + _HIGH + r"(?!\\u" + _LOW + ")"
_ANY_LOW = _ESCAPE + _LOW
_PAIR = _ESCAPE + _HIGH + r"\\u" + _LOW
_LONE_HIGH_BYTES = re.compile(_LONE_HIGH.encode("ascii"))
_ANY_LOW_BYTES = re.compile(_ANY_LOW.encode("ascii"))
_PAIR_BYTES = re.compile(_PAIR.encode("ascii"))
_RAW_SURROGATE_BYTES = re.compile(rb"\xed[\xa0-\xbf]")
_LONE_HIGH_TEXT = re.compile(_LONE_HIGH)
_ANY_LOW_TEXT = re.compile(_ANY_LOW)
_PAIR_TEXT = re.compile(_PAIR)


def _escapes_hold_a_lone_surrogate(
    text: Any, lone_high: re.Pattern, any_low: re.Pattern, pair: re.Pattern
) -> bool:
    if lone_high.search(text):
        return True
    lows = sum(1 for _ in any_low.finditer(text))
    return lows > 0 and lows > sum(1 for _ in pair.finditer(text))


_END = object()


def _raw_text_holds_a_lone_surrogate(body: bytes) -> bool:
    """Whether `json.loads(body)` would hold a lone surrogate, if `body` is JSON.

    Decided from the raw text, without parsing: a surrogate written as raw bytes, a high
    escape not followed by a low one, or more low escapes than pairs. Each search runs
    in C and allocates nothing that grows with the body; the counts step through the
    low escapes and the pairs one match at a time. So the cost is linear in the body's
    length and in its number of surrogate escapes, and does not depend on how many
    elements it holds or how deeply they nest. It is never False
    when the parsed value holds one. It can be True when the value does not: for a body
    that is not JSON, or whose surrogate is in a member a later duplicate key replaces;
    the parse that follows settles those.
    """
    encoding = json.detect_encoding(body)
    if encoding in ("utf-8", "utf-8-sig"):
        if not any(marker in body for marker in _MAY_HOLD_A_SURROGATE):
            return False
        if _RAW_SURROGATE_BYTES.search(body):
            return True
        return _escapes_hold_a_lone_surrogate(body, _LONE_HIGH_BYTES, _ANY_LOW_BYTES, _PAIR_BYTES)
    try:  # UTF-16 or UTF-32, decoded exactly as `json.loads` decodes it
        text = body.decode(encoding, "surrogatepass")
    except UnicodeDecodeError:
        return False
    if _LONE_SURROGATE.search(text):
        return True
    return _escapes_hold_a_lone_surrogate(text, _LONE_HIGH_TEXT, _ANY_LOW_TEXT, _PAIR_TEXT)


# The types `json.loads` produces. A container whose children are all of these is
# filtered in C before the walk sees it; any other type (a subclass, a non-JSON value)
# makes the walk visit every child of that container.
_JSON_TYPES = frozenset({str, dict, list, int, float, bool, type(None)})
_STR_TYPE = frozenset({str})
_CONTAINER_TYPES = frozenset({dict, list})
_STR_OR_CONTAINER_TYPES = frozenset({str, dict, list})
# Below this many children the filtering costs more than the plain loop it replaces.
_FILTER_FROM = 16


def _children(container: dict | list) -> Any:
    """The (key or index, child) pairs of a container the walk must visit, in order.

    A large container is filtered in C: numbers, booleans, and nulls are never yielded;
    strings only when one of them, joined, holds a lone surrogate; and when a key does,
    every child is yielded so the walk can find that key. So a long run of numbers or
    clean strings costs no Python step each.
    """
    if isinstance(container, dict):
        keys: Any = container
        values: Any = container.values()
        items: Any = container.items()
    else:
        keys = ()
        values = container
        items = enumerate(container)
    if len(container) < _FILTER_FROM:
        return iter(items)
    if not (set(map(type, values)) <= _JSON_TYPES and set(map(type, keys)) <= _STR_TYPE):
        return iter(items)
    if _LONE_SURROGATE.search("".join(keys)):
        return iter(items)
    strings = "".join(compress(values, map(_STR_TYPE.__contains__, map(type, values))))
    wanted = _STR_OR_CONTAINER_TYPES if _LONE_SURROGATE.search(strings) else _CONTAINER_TYPES
    return compress(items, map(wanted.__contains__, map(type, values)))


def lone_surrogate_locs(value: Any, limit: int | None = None) -> list[tuple[str | int, ...]]:
    """Where a parsed JSON value holds a lone surrogate, in document order, each once,
    stopping after `limit` locations when given.

    A string holding one is located at itself; a key holding one at the object that
    holds the key, and that key's value is not searched. A located key or index never
    holds a surrogate. One pass with a stack of iterators and one shared path, so memory
    grows with the nesting depth only, and a location tuple is built only for a hit;
    nothing recurses. Time is linear in the number of elements; in a large container
    the numbers, and the strings when none of them holds a hit, are passed over in C
    (`_children`), so the Python steps are about one per container and one per string
    of a container that holds a hit.
    """
    found: dict[tuple[str | int, ...], None] = {}

    def hit(loc: tuple[str | int, ...]) -> bool:
        found[loc] = None
        return limit is not None and len(found) >= limit

    if isinstance(value, str):
        return [()] if _LONE_SURROGATE.search(value) else []
    if not isinstance(value, (dict, list)):
        return []
    # A dict yields (key, value) with a str key, a list (index, value) with an int.
    path: list[str | int] = []
    stack: list[Any] = [_children(value)]
    while stack:
        entry = next(stack[-1], _END)
        if entry is _END:
            stack.pop()
            if stack:
                path.pop()
            continue
        key, item = entry
        if isinstance(key, str) and _LONE_SURROGATE.search(key):
            if hit(tuple(path)):
                break
            continue
        if isinstance(item, str):
            if _LONE_SURROGATE.search(item) and hit((*path, key)):
                break
        elif isinstance(item, (dict, list)):
            path.append(key)
            stack.append(_children(item))
    return list(found)


def holds_lone_surrogate(value: Any) -> bool:
    """Whether a parsed JSON value holds a lone surrogate in any string or key.

    Encodes the value once in C (`json.dumps` with `ensure_ascii=False`, then UTF-8,
    which refuses a lone surrogate), so the cost is that of serializing it; a value too
    deep to serialize, or one that is not JSON data, is searched with
    `lone_surrogate_locs` instead, which stops at the first location.
    """
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    except (RecursionError, TypeError, ValueError):
        return bool(lone_surrogate_locs(value, limit=1))
    return False


def body_lone_surrogate_errors(body: bytes) -> list[dict[str, Any]]:
    """The rendered `string_unicode` errors for a request body, empty when it is clean;
    at most `MAX_REPORTED_LOCATIONS`, the first ones in document order.

    The raw text decides whether the body can hold one; only then is it parsed, and the
    locations searched. Empty, too, for a body `json.loads` refuses: the route answers
    that as it would have (FastAPI's `json_invalid`).
    """
    if not _raw_text_holds_a_lone_surrogate(body):
        return []
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return []
    return render_validation_errors(
        {"type": STRING_UNICODE_TYPE, "loc": ("body", *loc), "msg": STRING_UNICODE_MSG}
        for loc in lone_surrogate_locs(parsed, limit=MAX_REPORTED_LOCATIONS)
    )


def _is_multipart(scope: Any) -> bool:
    for name, value in scope.get("headers", ()):
        if name == b"content-type":
            return value.lstrip().lower().startswith(b"multipart/")
    return False


class RefuseLoneSurrogates:
    """ASGI middleware: a request body holding a lone surrogate is a 422.

    It wraps `receive`. The first time the app reads the body, the whole body is read,
    checked with `body_lone_surrogate_errors`, and handed on as one message; when the
    check finds a surrogate, the read raises `HTTPException(422)` instead, which
    FastAPI's body reader passes on and the app's exception handling renders as
    `{"detail": [...]}`. A read made after the response has started (Starlette's
    disconnect listener on a route that never read its body) is handed on unchecked.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or _is_multipart(scope):
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
            errors = [] if started else body_lone_surrogate_errors(body)
            if errors:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors
                )
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, receive_checked, send_tracking_start)


def install_validation_errors(app: FastAPI, *, refuse_lone_surrogates: bool = True) -> None:
    """Use `validation_error_handler` for every 422 and advertise its shape, and,
    unless `refuse_lone_surrogates` is False (the config service alone), refuse a
    request body holding a lone surrogate with `RefuseLoneSurrogates`.

    The OpenAPI override is FastAPI's documented one: replace `app.openapi`
    with a function that builds the default document once, adjusts it, and
    caches it in `app.openapi_schema`.
    """
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    if refuse_lone_surrogates:
        # Innermost, next to the routes, whatever the order of the app's other
        # middleware: `add_middleware` would put it outermost, and a
        # BaseHTTPMiddleware (the request logger) runs the `receive` it passes on in a
        # task group, which wraps the refusal in an ExceptionGroup that FastAPI's body
        # reader turns into a 400. The same guard as `add_middleware`.
        if app.middleware_stack is not None:
            raise RuntimeError("Cannot add middleware after an application has started")
        app.user_middleware.append(Middleware(RefuseLoneSurrogates))

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = restrict_validation_error_schema(FastAPI.openapi(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
