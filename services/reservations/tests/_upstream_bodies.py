"""The bad upstream bodies every site of issue #1577 is tested against.

Every upstream answer the reservations service reads goes through
`herd_common.upstream_body.read_json_body`; these tables are the space of bodies a
site cannot use, as raw bytes served with `content-type: application/json` (what a
proxy, a sidecar, or a misdirected service URL could answer in a sibling's place).

- `UNPARSEABLE`: bodies the decoder refuses: empty, not JSON, not UTF-8, an
  integer past Python's digit limit (all `ValueError`), and an array and an object
  nested far past every interpreter's recursion limit (`RecursionError`; the depth
  at which the decoder raises differs between 3.12 and 3.14, #1487, so 100,000 is
  used).
- `NOT_AN_OBJECT`: bodies that parse to something other than a JSON object.

Each body that holds text carries `MARKER`, so a test can assert that nothing the
upstream sent reaches a response or a log line. `DECODER_TEXT` are fragments of the
decoder's own messages, which must not reach either.
"""

import json

import httpx

MARKER = "upstream-body-marker-1577"

DEPTH = 100_000

UNPARSEABLE: dict[str, bytes] = {
    "empty": b"",
    "not_json": f"<html>{MARKER} proxy error page</html>".encode(),
    "not_utf8": b'{"items": "' + MARKER.encode() + b"\xff\xfe" + b'"}',
    "huge_integer": b"9" * 5_000,
    "deep_array": b"[" * DEPTH + b"]" * DEPTH,
    "deep_object": b'{"a":' * DEPTH + b"1" + b"}" * DEPTH,
}

NOT_AN_OBJECT: dict[str, bytes] = {
    "list": json.dumps([1, 2, MARKER]).encode(),
    "string": json.dumps(MARKER).encode(),
    "integer": b"5",
    "float": b"1.5",
    "boolean": b"true",
    "null": b"null",
}

EVERY_BAD_BODY: dict[str, bytes] = {**UNPARSEABLE, **NOT_AN_OBJECT}

DECODER_TEXT = (
    "Expecting value",
    "codec can't decode",
    "maximum recursion depth",
    "Exceeds the limit",
    "object has no attribute",
    "indices must be integers",
    "not subscriptable",
)


def answer(status: int, body: bytes) -> httpx.Response:
    """A response carrying `body` as raw bytes under a JSON content type."""
    return httpx.Response(status, content=body, headers={"content-type": "application/json"})


def objects(*bodies: object) -> dict[str, bytes]:
    """Name each JSON value by its index, as bytes, for a table of wrong objects."""
    return {f"object_{i}": json.dumps(b).encode() for i, b in enumerate(bodies)}


def assert_nothing_leaked(*texts: str) -> None:
    """No upstream text and no decoder message in any of `texts`."""
    for text in texts:
        assert MARKER not in text
        for fragment in DECODER_TEXT:
            assert fragment not in text, fragment
