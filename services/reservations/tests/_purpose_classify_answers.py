"""Orchestrator answers the purpose classifier must map, one or more per answer class,
and the outcome each maps to (issue #1525; RES-PURPOSE-6, RES-PURPOSE-7, RES-PURPOSE-8,
RES-PURPOSE-13).

Covered: a 200 whose body is a usable suggestion (a JSON object with a string
`top_category`); a 200 whose body is not one (issue #1543): one that does not parse
(not JSON, empty, HTML, not UTF-8, or nested past the decoder's recursion limit, issue
#1577), every JSON type other than an object, an object without `top_category` or with
one of every other JSON type, an object nested deeper than the classifier stores, an
object holding a number that is not finite, and an object holding a NUL or an unpaired
surrogate in a value, a nested value, a key, a nested key, or `top_category` itself
(written as JSON escapes, so this file stays ASCII); beside those, the code points
that are stored (a surrogate pair, a noncharacter, a control character other than
NUL); a 403 with the disabled marker, and
403s without it (one nested past the decoder's recursion limit among them); a 404;
429, 502, 503, and 504; every other `http.HTTPStatus` member; a timeout; a transport
error other than a timeout.

The deep bodies nest `DEEP` levels, far past the decoder's limit on every interpreter
this runs on (about 10,000 on Python 3.12, where `sys.setrecursionlimit` does not move
it; the limit differs on 3.14, #1487), so they do not depend on the host's limit.

Most expected values come from the rule text in `docs/specs/reservations.md`, not from
`app/services/purpose_service.py`:

- RES-PURPOSE-6: a feature-off answer (403 with the disabled marker, or 404), a
  transient answer (429, 502, 503, 504, or a transport error other than a timeout), or
  a 403 without the marker ends the tick and counts no attempt.
- RES-PURPOSE-7: a timeout, any other non-200, or a 200 whose body is not a usable
  suggestion counts one attempt on that row and the tick continues.
- RES-PURPOSE-8: a 200 whose body is a usable suggestion stores it verbatim.
- RES-PURPOSE-13: Classify now answers 200 with the outcome word for every outcome
  except feature-off.

Three entries are decided by the code, not by a rule, since RES-PURPOSE-6 does not say
where the marker sits in a 403 body or which strings count as the marker:
`403-legacy-marker` (the legacy plain-string detail reads as the marker, as the
classifier's docstring and `test_reconcile_403_legacy_string_detail_is_still_feature_off`
state), `403-marker-under-another-key` (the marker read only under `detail`), and
`403-json-list` (a body that is not an object has no marker). The depth bound
(`200-object-nested-past-the-store-bound`), the non-finite numbers, and the NUL and
unpaired-surrogate strings are decided by the code too: the owner's rule on #1543 names
the object and its string `top_category`, and the classifier refuses these as well
because the store, Postgres's `json` operators, or the response models would refuse
them later (see `_suggestion_refusal`).

The outcome words are the six `classify_purpose_one`'s docstring names: `ok`,
`feature_off`, `forbidden`, `timeout`, `transient`, `failed`.

Enumerated from the library rather than listed by hand, so a member added later is
covered without an edit here: every `http.HTTPStatus` member the rules do not name is
"any other non-200" (each sent with a well-formed suggestion body, so a status read as
a success would store it); every subclass of `httpx.TimeoutException` defined in the
`httpx` package is a timeout; every other subclass of `httpx.TransportError` defined
in the `httpx` package, plus `ProbeTransportError`, is a transport error other than a
timeout.

Kept out of the test files so the single-row tests, the sweep tests, and the
Classify now tests run over the same table.
"""

from __future__ import annotations

import http
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

OUTCOME_WORDS = frozenset({"ok", "feature_off", "forbidden", "timeout", "transient", "failed"})

# The outcomes RES-PURPOSE-6 says end the tick.
TICK_ENDING = frozenset({"feature_off", "transient", "forbidden"})
# The outcomes RES-PURPOSE-7 says count one attempt on the row.
COUNTS_AN_ATTEMPT = frozenset({"timeout", "failed"})

SUGGESTION_BODY = {
    "distribution": [
        {"category": "qa_regression", "probability": 0.8},
        {"category": "training", "probability": 0.2},
    ],
    "top_category": "qa_regression",
    "pass": "end",
    "model": "test-model",
    "rationale": "r",
    "generated_at": datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc).isoformat(),
    "signals_used": ["purpose_text"],
}

# Far past the JSON decoder's recursion limit on every interpreter (see the docstring).
DEEP = 200_000
# RES-PURPOSE-7: the deepest nesting a stored suggestion may have.
STORE_DEPTH_BOUND = 32


def _nested_in_lists(levels: int) -> object:
    """A value `levels` lists deep (0 is a bare string)."""
    value: object = "x"
    for _ in range(levels):
        value = [value]
    return value


def _deep_array() -> bytes:
    return b"[" * DEEP + b"]" * DEEP


_DROP = object()


def _with(**fields) -> dict:
    """SUGGESTION_BODY with `fields` replaced (a field set to _DROP is removed)."""
    body = {**SUGGESTION_BODY, **fields}
    return {k: v for k, v in body.items() if v is not _DROP}


FEATURE_OFF_DETAIL = {
    "error": "purpose_classification_disabled",
    "message": "Purpose classification is disabled",
}
LEGACY_FEATURE_OFF_DETAIL = "Purpose classification is disabled"

# The status codes RES-PURPOSE-6 and RES-PURPOSE-8 name; every other status is
# "any other non-200" (RES-PURPOSE-7).
_NAMED_STATUSES = frozenset({200, 403, 404, 429, 502, 503, 504})
TRANSIENT_STATUSES = (429, 502, 503, 504)


class ProbeTransportError(httpx.TransportError):
    """A transport error of this test's own, not one of httpx's named classes, so a
    handler narrowed to the classes httpx names today stops catching it."""


class ProbeUnexpectedError(Exception):
    """Not an httpx error at all: an exception from inside the call that no
    transport class covers (call_service raises RuntimeError for a missing token)."""


def _subclasses(root: type) -> list[type]:
    out = [root]
    for sub in root.__subclasses__():
        out.extend(_subclasses(sub))
    return out


def _defined_in_httpx(cls: type) -> bool:
    """True for a class of the `httpx` package itself, not of a package whose name
    merely starts with it (such as `httpx2`)."""
    return cls.__module__ == "httpx" or cls.__module__.startswith("httpx.")


def _timeout_classes() -> list[type]:
    return [c for c in _subclasses(httpx.TimeoutException) if _defined_in_httpx(c)]


def _non_timeout_transport_classes() -> list[type]:
    timeouts = set(_subclasses(httpx.TimeoutException))
    named = [
        c for c in _subclasses(httpx.TransportError) if c not in timeouts and _defined_in_httpx(c)
    ]
    return [*named, ProbeTransportError]


@dataclass(frozen=True)
class Answer:
    """One orchestrator answer and the outcome the rules give it."""

    id: str
    make: Callable[[], httpx.Response | BaseException]
    outcome: str
    stores: dict | None = None

    @property
    def ends_tick(self) -> bool:
        return self.outcome in TICK_ENDING

    @property
    def counts_attempt(self) -> bool:
        return self.outcome in COUNTS_AN_ATTEMPT


def _response(status: int, **kwargs) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(status, **kwargs)


def _raise(cls: type) -> Callable[[], BaseException]:
    return lambda: cls("probe")


def _answers() -> list[Answer]:
    cases = [
        Answer("200-suggestion", _response(200, json=SUGGESTION_BODY), "ok", SUGGESTION_BODY),
        # Unparseable bodies (RES-PURPOSE-7).
        Answer("200-not-json", _response(200, content=b"not json"), "failed"),
        Answer("200-empty-body", _response(200, content=b""), "failed"),
        Answer("200-html", _response(200, content=b"<html>"), "failed"),
        Answer("200-invalid-utf8", _response(200, content=b"\xff\xfe\xfa"), "failed"),
        # Nested past the decoder's recursion limit (#1577): an array, and an object
        # whose top_category is a string but whose other field is too deep.
        Answer("200-deep-array", lambda: httpx.Response(200, content=_deep_array()), "failed"),
        Answer(
            "200-deep-object",
            lambda: httpx.Response(
                200,
                content=b'{"top_category": "training", "x": ' + _deep_array() + b"}",
            ),
            "failed",
        ),
        # Parses, but not a JSON object (#1543).
        Answer("200-json-list", _response(200, json=[1, 2]), "failed"),
        Answer("200-json-list-of-suggestion", _response(200, json=[SUGGESTION_BODY]), "failed"),
        Answer("200-json-string", _response(200, json="just a string"), "failed"),
        Answer("200-json-integer", _response(200, json=7), "failed"),
        Answer("200-json-float", _response(200, json=1.5), "failed"),
        Answer("200-json-true", _response(200, json=True), "failed"),
        Answer("200-json-false", _response(200, json=False), "failed"),
        Answer("200-json-null", _response(200, content=b"null"), "failed"),
        # An object, but without a string top_category (#1543, the owner's decision).
        Answer("200-empty-object", _response(200, json={}), "failed"),
        Answer("200-no-top-category", _response(200, json=_with(top_category=_DROP)), "failed"),
        Answer("200-top-category-null", _response(200, json=_with(top_category=None)), "failed"),
        Answer("200-top-category-integer", _response(200, json=_with(top_category=7)), "failed"),
        Answer("200-top-category-float", _response(200, json=_with(top_category=1.5)), "failed"),
        Answer("200-top-category-true", _response(200, json=_with(top_category=True)), "failed"),
        Answer(
            "200-top-category-list",
            _response(200, json=_with(top_category=["qa_regression"])),
            "failed",
        ),
        Answer(
            "200-top-category-object",
            _response(200, json=_with(top_category={"name": "qa_regression"})),
            "failed",
        ),
        # The store bound: an object exactly STORE_DEPTH_BOUND deep is stored, one level
        # deeper is not (the object itself is the first level).
        Answer(
            "200-object-at-the-store-bound",
            _response(200, json=_with(x=_nested_in_lists(STORE_DEPTH_BOUND - 1))),
            "ok",
            _with(x=_nested_in_lists(STORE_DEPTH_BOUND - 1)),
        ),
        Answer(
            "200-object-nested-past-the-store-bound",
            _response(200, json=_with(x=_nested_in_lists(STORE_DEPTH_BOUND))),
            "failed",
        ),
        # A number that is not finite: none is JSON, and Postgres refuses the stored text.
        Answer(
            "200-nan",
            _response(200, content=b'{"top_category": "training", "p": NaN}'),
            "failed",
        ),
        Answer(
            "200-infinity",
            _response(200, content=b'{"top_category": "training", "p": [Infinity]}'),
            "failed",
        ),
        Answer(
            "200-minus-infinity",
            _response(200, content=b'{"top_category": "training", "p": {"q": -Infinity}}'),
            "failed",
        ),
        Answer(
            "200-overflowing-float",
            _response(200, content=b'{"top_category": "training", "p": 1e400}'),
            "failed",
        ),
    ]
    # A NUL or an unpaired surrogate anywhere in the object (#1543 review): in a value,
    # a nested value, a key, a nested key, and top_category itself. Each is written as
    # a JSON escape inside the body template, so this file stays ASCII.
    templates = {
        "value": '{"top_category": "training", "r": "a%sb"}',
        "nested-value": '{"top_category": "training", "d": [{"category": "a%sb"}]}',
        "key": '{"top_category": "training", "a%sb": 1}',
        "nested-key": '{"top_category": "training", "d": [{"a%sb": 1}]}',
        "top-category": '{"top_category": "training%s"}',
    }
    escapes = {"nul": "\\u0000", "high-surrogate": "\\ud800", "low-surrogate": "\\udc00"}
    for text, escape in escapes.items():
        for where, template in templates.items():
            body = (template % escape).encode()
            cases.append(Answer(f"200-{text}-in-{where}", _response(200, content=body), "failed"))
    # The code points beside them that are stored: a surrogate pair (one code point), a
    # noncharacter, and a control character other than NUL.
    for name, escape, decoded in (
        ("surrogate-pair", "\\ud83d\\ude00", "\U0001f600"),
        ("noncharacter", "\\uffff", "\uffff"),
        ("control-character", "\\u0001", "\x01"),
    ):
        body = '{"top_category": "training", "r": "%s", "%s": 1}' % (escape, escape)
        cases.append(
            Answer(
                f"200-{name}-is-stored",
                _response(200, content=body.encode()),
                "ok",
                {"top_category": "training", "r": decoded, decoded: 1},
            )
        )
    cases += [
        # 403 with the marker, and the legacy plain-string marker (RES-PURPOSE-6).
        Answer("403-marker", _response(403, json={"detail": FEATURE_OFF_DETAIL}), "feature_off"),
        Answer(
            "403-legacy-marker",
            _response(403, json={"detail": LEGACY_FEATURE_OFF_DETAIL}),
            "feature_off",
        ),
        # 403 without the marker (RES-PURPOSE-6).
        Answer(
            "403-other-string-detail",
            _response(403, json={"detail": "Invalid internal token"}),
            "forbidden",
        ),
        Answer(
            "403-other-dict-detail",
            _response(403, json={"detail": {"error": "something_else"}}),
            "forbidden",
        ),
        Answer(
            "403-marker-under-another-key",
            _response(403, json={"error": "purpose_classification_disabled"}),
            "forbidden",
        ),
        Answer("403-json-list", _response(403, json=[FEATURE_OFF_DETAIL]), "forbidden"),
        Answer("403-html", _response(403, content=b"<html>forbidden</html>"), "forbidden"),
        Answer("403-empty-body", _response(403, content=b""), "forbidden"),
        Answer("403-invalid-utf8", _response(403, content=b"\xff\xfe\xfa"), "forbidden"),
        Answer("403-deep-array", lambda: httpx.Response(403, content=_deep_array()), "forbidden"),
        # 404 whatever the body (RES-PURPOSE-6).
        Answer("404-json", _response(404, json={"detail": "Not Found"}), "feature_off"),
        Answer("404-html", _response(404, content=b"<html>"), "feature_off"),
    ]
    cases += [
        Answer(f"{code}-transient", _response(code, text="unavailable"), "transient")
        for code in TRANSIENT_STATUSES
    ]
    # Each carries a well-formed suggestion body, so a status read as a success
    # (any 2xx, say) would store it rather than fail on the body.
    cases += [
        Answer(f"{s.value}-other-non-200", _response(s.value, json=SUGGESTION_BODY), "failed")
        for s in http.HTTPStatus
        if s.value not in _NAMED_STATUSES
    ]
    cases += [Answer(f"raise-{c.__name__}", _raise(c), "timeout") for c in _timeout_classes()]
    cases += [
        Answer(f"raise-{c.__name__}", _raise(c), "transient")
        for c in _non_timeout_transport_classes()
    ]
    return cases


ANSWERS: list[Answer] = _answers()
ANSWER_IDS = [a.id for a in ANSWERS]


def deliver(answer: Answer) -> httpx.Response:
    """Return the answer's response, or raise its exception (for a fake call_service)."""
    made = answer.make()
    if isinstance(made, BaseException):
        raise made
    return made
