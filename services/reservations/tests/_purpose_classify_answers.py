"""Every class of orchestrator answer the purpose classifier must map, and what it
maps to (issue #1525; RES-PURPOSE-6, RES-PURPOSE-7, RES-PURPOSE-8, RES-PURPOSE-13).

The expected values come from the rule text in `docs/specs/reservations.md`, not from
`app/services/purpose_service.py`:

- RES-PURPOSE-6: a feature-off answer (403 with the disabled marker, or 404), a
  transient answer (429, 502, 503, 504, or a transport error other than a timeout), or
  a 403 without the marker ends the tick and counts no attempt.
- RES-PURPOSE-7: a timeout, any other non-200, or an unparseable body counts one
  attempt on that row and the tick continues.
- RES-PURPOSE-8: a 200 stores the body verbatim as the suggestion.
- RES-PURPOSE-13: Classify now answers 200 with the outcome word for every outcome
  except feature-off.

The outcome words are the six `classify_purpose_one`'s docstring names: `ok`,
`feature_off`, `forbidden`, `timeout`, `transient`, `failed`.

Enumerated from the library rather than listed by hand, so a member added later is
covered without an edit here: every `http.HTTPStatus` member the rules do not name is
"any other non-200" (each sent with a well-formed suggestion body, so a status read as
a success would store it); every subclass of `httpx.TimeoutException` that httpx
defines is a timeout; every other subclass of `httpx.TransportError` that httpx
defines, plus `ProbeTransportError`, is a transport error other than a timeout.
The legacy plain-string 403 detail is read as the disabled marker, as the classifier's
docstring and `test_reconcile_403_legacy_string_detail_is_still_feature_off` state.

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


def _timeout_classes() -> list[type]:
    return [c for c in _subclasses(httpx.TimeoutException) if c.__module__.startswith("httpx")]


def _non_timeout_transport_classes() -> list[type]:
    timeouts = set(_subclasses(httpx.TimeoutException))
    named = [
        c
        for c in _subclasses(httpx.TransportError)
        if c not in timeouts and c.__module__.startswith("httpx")
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
