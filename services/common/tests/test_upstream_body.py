"""herd_common.upstream_body.read_json_body (issue #1577).

The helper's contract, row by row: a body the decoder refuses is `not_json` or
`too_deep`, a parsed body that is not an instance of `expect` is `wrong_type`, and
anything else is returned as parsed. The exception carries only its fixed reason:
no body bytes in its text, and neither `__cause__` nor `__context__` holds the
decoder's exception (a `JSONDecodeError` keeps the whole body as its `doc`).
"""

import json

import httpx
import pytest
from herd_common.upstream_body import (
    NOT_JSON,
    REASONS,
    TOO_DEEP,
    WRONG_FIELD,
    WRONG_TYPE,
    UpstreamBodyUnusable,
    read_json_body,
)

MARKER = "upstream-body-marker"
# Far past every interpreter's recursion limit: the depth at which `json` raises
# differs between 3.12 and 3.14 (#1487, #1538).
DEPTH = 100_000

UNPARSEABLE = {
    "empty": (b"", NOT_JSON),
    "not_json": (f"<html>{MARKER}</html>".encode(), NOT_JSON),
    "truncated": (b'{"a": "' + MARKER.encode(), NOT_JSON),
    "not_utf8": (b'{"a": "' + MARKER.encode() + b'\xff\xfe"}', NOT_JSON),
    "huge_integer": (b"9" * 5_000, NOT_JSON),
    "deep_array": (b"[" * DEPTH + b"]" * DEPTH, TOO_DEEP),
    "deep_object": (b'{"a":' * DEPTH + b"1" + b"}" * DEPTH, TOO_DEEP),
}

PARSED = {
    "object": {"a": MARKER},
    "list": [MARKER],
    "string": MARKER,
    "integer": 5,
    "float": 1.5,
    "boolean": True,
    "null": None,
}

# For each `expect`, the PARSED names it admits; every other parsed value is wrong_type.
ADMITS = {
    "dict": (dict, {"object"}),
    "list": (list, {"list"}),
    "dict_or_none": ((dict, type(None)), {"object", "null"}),
    "object": (object, set(PARSED)),
}


def _response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


def _assert_carries_nothing(exc: UpstreamBodyUnusable) -> None:
    assert str(exc) == f"upstream body unusable: {exc.reason}"
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert MARKER not in repr(exc)


@pytest.mark.parametrize("expect_name", ADMITS)
@pytest.mark.parametrize(("body", "reason"), UNPARSEABLE.values(), ids=UNPARSEABLE.keys())
def test_a_body_the_decoder_refuses_is_its_fixed_reason_for_every_expect(body, reason, expect_name):
    with pytest.raises(UpstreamBodyUnusable) as caught:
        read_json_body(_response(body), expect=ADMITS[expect_name][0])
    assert caught.value.reason == reason
    _assert_carries_nothing(caught.value)


@pytest.mark.parametrize("expect_name", ADMITS)
@pytest.mark.parametrize("value_name", PARSED)
def test_a_parsed_body_is_returned_or_wrong_type_by_expect(value_name, expect_name):
    expect, admitted = ADMITS[expect_name]
    resp = _response(json.dumps(PARSED[value_name]).encode())
    if value_name in admitted:
        assert read_json_body(resp, expect=expect) == PARSED[value_name]
        return
    with pytest.raises(UpstreamBodyUnusable) as caught:
        read_json_body(resp, expect=expect)
    assert caught.value.reason == WRONG_TYPE
    _assert_carries_nothing(caught.value)


def test_the_reasons_are_the_four_fixed_codes_and_no_other_is_accepted():
    assert REASONS == {NOT_JSON, TOO_DEEP, WRONG_TYPE, WRONG_FIELD}
    assert UpstreamBodyUnusable(WRONG_FIELD).reason == WRONG_FIELD
    with pytest.raises(ValueError):
        UpstreamBodyUnusable(MARKER)


def test_it_parses_through_the_responses_own_json_method():
    """A stub that defines only `json(**kwargs)` works, as the services' unit tests
    use (MagicMock included); the helper passes its `parse_constant` hook."""
    seen: dict = {}

    class Stub:
        def json(self, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

    assert read_json_body(Stub(), expect=dict) == {"ok": True}
    assert set(seen) == {"parse_constant", "parse_float"}


# Bodies the decoder accepts that no response can render again (`allow_nan=False`,
# then UTF-8): each is not_json for every expect.
UNRENDERABLE = {
    "nan": b'{"a": NaN}',
    "infinity": b"[Infinity]",
    "minus_infinity": b"-Infinity",
    "nested_nan": b'{"a": [1, {"b": NaN}]}',
    "overflowing_float": b'{"a": 1e400}',
    "overflowing_negative_float": b"[-1.5e999]",
    "lone_high_surrogate": b'{"a": "\\ud800"}',
    "lone_low_surrogate": b'"\\udfff"',
    "lone_surrogate_key": b'{"\\udc00": 1}',
    "nested_lone_surrogate": b'[[{"a": ["x\\ud800y"]}]]',
}


@pytest.mark.parametrize("expect_name", ADMITS)
@pytest.mark.parametrize("body", UNRENDERABLE.values(), ids=UNRENDERABLE.keys())
def test_a_non_finite_constant_or_a_lone_surrogate_is_not_json(body, expect_name):
    with pytest.raises(UpstreamBodyUnusable) as caught:
        read_json_body(_response(body), expect=ADMITS[expect_name][0])
    assert caught.value.reason == NOT_JSON
    _assert_carries_nothing(caught.value)


@pytest.mark.parametrize(
    ("body", "value"),
    [
        (b'{"a": "\\ud83d\\ude00"}', {"a": "\U0001f600"}),
        (b'{"a": 1e308, "b": -0.0, "c": "NaN"}', {"a": 1e308, "b": -0.0, "c": "NaN"}),
    ],
    ids=["surrogate_pair", "finite_numbers_and_the_text_nan"],
)
def test_a_surrogate_pair_and_finite_numbers_are_honest(body, value):
    """The honest neighbours of the table above are returned as parsed."""
    assert read_json_body(_response(body), expect=dict) == value


def test_the_table_is_not_vacuous():
    """Each decoder failure class and each JSON type is in the tables."""
    assert {reason for _, reason in UNPARSEABLE.values()} == {NOT_JSON, TOO_DEEP}
    assert {type(v).__name__ for v in PARSED.values()} == {
        "dict",
        "list",
        "str",
        "int",
        "float",
        "bool",
        "NoneType",
    }
