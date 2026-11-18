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
  pydantic's own `string_unicode` type and message. `loc` is `["body", ...]` and ends
  at the string that holds the surrogate, or, for a key, at the object holding the key
  (as pydantic refuses such a key on an `extra="forbid"` body); it never holds the
  surrogate or the value. At most `MAX_REPORTED_LOCATIONS` (10) locations are
  reported, the first in document order that the search reaches within
  `LOCATION_SEARCH_BUDGET` steps, each whose parts total at most `MAX_LOC_CHARS`
  characters and all within `MAX_LOCS_CHARS` (a location is left out whole, never cut);
  with none to report, the answer is one item at `["body"]`. Nothing marks that more
  were left out.
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
  A body the scan says holds one is parsed, and if it is JSON it is refused, always:
  also when the surrogate is in a member a later duplicate key replaces, which no
  honest client sends. The search for locations is bounded by its step budget and runs
  with the cyclic collector paused. So the cost added to a body the route would have
  parsed anyway is the scan plus the budgeted search; it runs on the event loop, like
  FastAPI's own parse. The measured costs are in OPS-VALID-8
  (`docs/specs/operations-and-observability.md`); they are observations, not a bound.
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

import gc
import json
import re
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
# The most steps (children taken up, of any type) the location search may spend on one
# body. A body that holds a lone surrogate is refused whether or not the search finds
# it; the search only names where, exactly for a body of up to this many elements and
# for a surrogate among its first this many otherwise. Measured on a development host
# pinned to one core, a step costs 0.2 to 0.3 microseconds, so the search takes at most
# about 6 ms, whatever the body.
LOCATION_SEARCH_BUDGET = 20_000
# The longest `loc`, in characters of its parts, one reported item may carry, and the
# most characters all reported `loc`s may carry together. A longer `loc` is left out
# whole, never cut (a cut key would name a wrong place), so a body cannot make the
# answer larger than about this, whatever its keys.
MAX_LOC_CHARS = 256
MAX_LOCS_CHARS = 2048

# Bytes that must be present for a UTF-8 JSON text to hold a lone surrogate: the start
# of an escape whose first hex digit is D, or the lead byte 0xED of a surrogate written
# as raw UTF-8 bytes (`json.loads` decodes with `surrogatepass`).
_MAY_HOLD_A_SURROGATE = (b"\\ud", b"\\uD", b"\xed")

# The JSON escape grammar, for the raw scan. A backslash starts an escape only when the
# run of backslashes it ends is odd. `json.loads` joins a high-surrogate escape
# immediately followed by a low-surrogate escape into one code point; every other
# surrogate escape, and every raw surrogate, stays a lone surrogate in the parsed value.
#
# A lone high is found by a search that starts at the first backslash of a run (a
# backslash not preceded by one: the literal first lets the engine skip ahead), takes
# the run's escaped-backslash pairs possessively, and needs a high escape not followed
# by a low one, so `\\ud800` (an escaped backslash, then text) is no escape and
# `\\\ud800` is. A lone low is found by one anchored match that reads the text from its
# start as escapes, possessively: runs of other bytes, a backslash and the character it
# escapes, a pair, or the `\u` of any other escape, until a low escape no pair took;
# it runs only when the text holds a low escape at all. Both run in C,
# never backtrack, and allocate nothing that grows with the body.
_HEX = "[0-9a-fA-F]"
_HIGH = "[dD][89abAB]" + _HEX * 2
_LOW = "[dD][c-fC-F]" + _HEX * 2
_ESCAPE = r"\\(?<!\\\\)(?:\\\\)*+u"
_LONE_HIGH = _ESCAPE + _HIGH + r"(?!\\u" + _LOW + ")"
_BARE_LOW = r"(?:[^\\]++|\\[^u]|\\u" + _HIGH + r"\\u" + _LOW + r"|\\u(?!" + _LOW + r"))*+\\u" + _LOW
# A low escape's text, anywhere: without one there is no lone low to look for.
_LOW_TEXT = r"\\u" + _LOW
_LONE_HIGH_BYTES = re.compile(_LONE_HIGH.encode("ascii"))
_BARE_LOW_BYTES = re.compile(_BARE_LOW.encode("ascii"))
_LOW_TEXT_BYTES = re.compile(_LOW_TEXT.encode("ascii"))
_RAW_SURROGATE_BYTES = re.compile(rb"\xed[\xa0-\xbf]")
_LONE_HIGH_TEXT = re.compile(_LONE_HIGH)
_BARE_LOW_TEXT = re.compile(_BARE_LOW)
_LOW_TEXT_TEXT = re.compile(_LOW_TEXT)


def _escapes_hold_a_lone_surrogate(
    text: Any, lone_high: re.Pattern, low_text: re.Pattern, bare_low: re.Pattern
) -> bool:
    if lone_high.search(text) is not None:
        return True
    return low_text.search(text) is not None and bare_low.match(text) is not None


_END = object()


def _raw_text_holds_a_lone_surrogate(body: bytes) -> bool:
    """Whether `json.loads(body)` would hold a lone surrogate, if `body` is JSON.

    Decided from the raw text, without parsing: a surrogate written as raw bytes, a high
    escape not followed by a low one, or a low escape no pair holds. Each is one pass
    in C that allocates nothing that grows with the body, so the cost is linear in the
    body's length and does not depend on how many elements it holds or how deeply they
    nest. It is never False when the parsed value holds one. It can be True when the
    value does not: for a body that is not JSON, which the parse that follows passes
    through, and for one whose surrogate is in a member a later duplicate key replaces,
    which is refused all the same.
    """
    encoding = json.detect_encoding(body)
    if encoding in ("utf-8", "utf-8-sig"):
        if not any(marker in body for marker in _MAY_HOLD_A_SURROGATE):
            return False
        if _RAW_SURROGATE_BYTES.search(body):
            return True
        return _escapes_hold_a_lone_surrogate(
            body, _LONE_HIGH_BYTES, _LOW_TEXT_BYTES, _BARE_LOW_BYTES
        )
    try:  # UTF-16 or UTF-32, decoded exactly as `json.loads` decodes it
        text = body.decode(encoding, "surrogatepass")
    except UnicodeDecodeError:
        return False
    if _LONE_SURROGATE.search(text):
        return True
    return _escapes_hold_a_lone_surrogate(text, _LONE_HIGH_TEXT, _LOW_TEXT_TEXT, _BARE_LOW_TEXT)


def _items(container: dict | list) -> Any:
    """The (key or index, child) pairs of a container, in order."""
    return iter(container.items()) if isinstance(container, dict) else enumerate(container)


def lone_surrogate_locs(
    value: Any, limit: int | None = None, budget: int | None = None
) -> list[tuple[str | int, ...]]:
    """Where a parsed JSON value holds a lone surrogate, in document order, each once,
    stopping after `limit` locations, or after `budget` steps (children taken up, of
    any type), when given: then the locations found so far, which may be none.

    A string holding one is located at itself; a key holding one at the object that
    holds the key, and that key's value is not searched. A located key or index never
    holds a surrogate. One pass with a stack of iterators and one shared path, so memory
    grows with the nesting depth only, and a location tuple is built only for a hit;
    nothing recurses. Time is one step per child taken up, so with a budget it is
    bounded whatever the value.
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
    stack: list[Any] = [_items(value)]
    steps = 0
    while stack:
        entry = next(stack[-1], _END)
        if entry is _END:
            stack.pop()
            if stack:
                path.pop()
            continue
        if budget is not None:
            steps += 1
            if steps > budget:
                break
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
            stack.append(_items(item))
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


def _reportable(locs: list[tuple[str | int, ...]]) -> list[tuple[str | int, ...]]:
    """The locations an answer may carry: each whose parts total at most
    `MAX_LOC_CHARS` characters, while all of them together stay within
    `MAX_LOCS_CHARS`. A location is kept or left out whole."""
    kept: list[tuple[str | int, ...]] = []
    total = 0
    for loc in locs:
        size = 0
        for part in loc:
            size += len(part) if isinstance(part, str) else len(str(part))
            if size > MAX_LOC_CHARS:
                break
        if size > MAX_LOC_CHARS or total + size > MAX_LOCS_CHARS:
            continue
        kept.append(loc)
        total += size
    return kept


def body_lone_surrogate_errors(body: bytes) -> list[dict[str, Any]]:
    """The rendered `string_unicode` errors for a request body, empty when it is clean.

    The raw text decides whether the body holds a lone surrogate. If it does and the
    body is JSON, the body is refused, always: also when the surrogate is in a member a
    later duplicate key replaces, which is never honest input. The locations are then
    searched within `LOCATION_SEARCH_BUDGET` steps, at most `MAX_REPORTED_LOCATIONS`,
    and kept within `MAX_LOC_CHARS` and `MAX_LOCS_CHARS`; with none to report, the
    answer is one item at `["body"]`. Empty for a body `json.loads` refuses: the route
    answers that as it would have (FastAPI's `json_invalid`).
    """
    if not _raw_text_holds_a_lone_surrogate(body):
        return []
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return []
    # The search runs with the cyclic garbage collector paused, and the parsed value is
    # released before it resumes. The parse leaves every container it made pending for
    # a full collection, which the search's own small allocations would set off: on a
    # 1 MB body of nested lists that collection cost more than the parse. Paused, the
    # collection that follows finds the value already gone. The pause lasts the search
    # (bounded by LOCATION_SEARCH_BUDGET) and the release, never the parse, and puts the
    # collector back as it was.
    collecting = gc.isenabled()
    gc.disable()
    try:
        found = lone_surrogate_locs(
            parsed, limit=MAX_REPORTED_LOCATIONS, budget=LOCATION_SEARCH_BUDGET
        )
        del parsed
    finally:
        if collecting:
            gc.enable()
    locs = _reportable(found) or [()]
    return render_validation_errors(
        {"type": STRING_UNICODE_TYPE, "loc": ("body", *loc), "msg": STRING_UNICODE_MSG}
        for loc in locs
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
