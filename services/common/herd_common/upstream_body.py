"""The ONE guarded read of a JSON body another HERD service answered (issue #1577).

A caller that has an `httpx.Response` from a sibling service and needs its JSON
body calls `read_json_body(resp, expect=...)` instead of `resp.json()`. The
helper turns every body it cannot hand back as the expected top-level type into
ONE exception, `UpstreamBodyUnusable`, whose `reason` is a fixed code:

- `not_json`: the body is not JSON or not UTF-8 (`json.JSONDecodeError` and
  `UnicodeDecodeError`, both `ValueError`), holds an integer longer than
  Python's digit limit (also a `ValueError`), holds `NaN`, `Infinity`, or
  `-Infinity` (not JSON; Python's decoder accepts them unless told not to) or a
  number literal that overflows to infinity (`1e400`), or
  holds a string or key with a lone surrogate (an escape such as `"\\ud800"`
  decodes to one). The last two parse but cannot be rendered again: a relay
  that passed one on would fail in its own response. No HERD service can send
  either, since every JSON response is rendered with `allow_nan=False` and
  encoded as UTF-8.
- `too_deep`: the body is nested deeper than the decoder's recursion limit
  (`RecursionError`, which is a `RuntimeError`, not a `ValueError`, so a catch
  of `ValueError` alone misses it). The depth at which the decoder raises
  differs between interpreters (#1487, #1538); the helper does not depend on it.
- `wrong_type`: the body parsed, but its top-level value is not an instance of
  `expect` (a list, a string, a number, a boolean, or JSON `null` where an
  object is expected, and so on).

A caller that checks the fields it needs raises the same exception with
`wrong_field` for a missing or wrongly typed field, so each site maps one type
to its documented failure path (a fixed 503, a closed or open answer, a
skipped and logged row).

The exception never carries body text: its message is the fixed reason, and it
is raised outside the decoder's `except` block, so neither `__cause__` nor
`__context__` holds the decoder's exception (whose `doc` attribute is the whole
body). Log it by `reason` or class name; never put the body, `resp.text`, or a
decoder message in a row, a response, or a log line.
"""

from __future__ import annotations

import math
from typing import Any

from herd_common.validation_errors import holds_lone_surrogate

NOT_JSON = "not_json"
TOO_DEEP = "too_deep"
WRONG_TYPE = "wrong_type"
WRONG_FIELD = "wrong_field"

REASONS = frozenset({NOT_JSON, TOO_DEEP, WRONG_TYPE, WRONG_FIELD})


class UpstreamBodyUnusable(Exception):
    """A sibling service answered a body its caller cannot use.

    `reason` is one of `REASONS`; the message is fixed text built from it, so
    `str(exc)` never holds anything the upstream sent.
    """

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        super().__init__(f"upstream body unusable: {reason}")
        self.reason = reason


def read_json_body(resp: Any, *, expect: type | tuple[type, ...]) -> Any:
    """Parse `resp`'s JSON body and return it when it is an instance of `expect`.

    `expect` is the top-level type the caller needs: `dict` for an object,
    `list` for an array, or `object` to take any JSON value. Raises
    `UpstreamBodyUnusable` with `not_json`, `too_deep`, or `wrong_type`
    otherwise. The body is parsed through `resp.json(parse_constant=...)`, so a
    response stub whose `json` takes keyword arguments works the same as a real
    `httpx.Response`.
    """
    reason: str | None = None
    try:
        body = resp.json(parse_constant=_refuse_constant, parse_float=_finite_float)
    except ValueError:
        reason = NOT_JSON
    except RecursionError:
        reason = TOO_DEEP
    # Raised here, outside the except blocks, so the decoder's exception (which
    # carries the body as its `doc`) is neither the cause nor the context.
    if reason is not None:
        raise UpstreamBodyUnusable(reason)
    if holds_lone_surrogate(body):
        raise UpstreamBodyUnusable(NOT_JSON)
    if not isinstance(body, expect):
        raise UpstreamBodyUnusable(WRONG_TYPE)
    return body


def _refuse_constant(name: str) -> Any:
    """`json.loads`'s hook for `NaN`, `Infinity`, and `-Infinity`, which are not
    JSON: a ValueError, so such a body is `not_json`. The name is not quoted."""
    raise ValueError("non-finite constant")


def _finite_float(text: str) -> float:
    """`json.loads`'s hook for a number with a fraction or an exponent: a literal
    such as `1e400` reads as infinity, which no response can render again, so it is
    a ValueError (`not_json`) like `Infinity` itself."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value
