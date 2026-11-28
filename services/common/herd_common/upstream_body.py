"""The ONE guarded read of a JSON body another HERD service answered (issue #1577).

A caller that has an `httpx.Response` from a sibling service and needs its JSON
body calls `read_json_body(resp, expect=...)` instead of `resp.json()`. The
helper turns every body it cannot hand back as the expected top-level type into
ONE exception, `UpstreamBodyUnusable`, whose `reason` is a fixed code:

- `not_json`: the body is not JSON or not UTF-8 (`json.JSONDecodeError` and
  `UnicodeDecodeError`, both `ValueError`), or holds an integer longer than
  Python's digit limit (also a `ValueError`).
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

from typing import Any

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
    otherwise. The body is parsed through `resp.json()`, so a response stub that
    defines only `json()` works the same as a real `httpx.Response`.
    """
    reason: str | None = None
    try:
        body = resp.json()
    except ValueError:
        reason = NOT_JSON
    except RecursionError:
        reason = TOO_DEEP
    # Raised here, outside the except blocks, so the decoder's exception (which
    # carries the body as its `doc`) is neither the cause nor the context.
    if reason is not None:
        raise UpstreamBodyUnusable(reason)
    if not isinstance(body, expect):
        raise UpstreamBodyUnusable(WRONG_TYPE)
    return body
