"""Unit tests for the contract suite's OpenAPI fetch retry helper.

These pin the flaky-fetch fix from issue #199: the gateway can transiently answer a
200 with an empty or not-yet-valid-JSON body while an upstream warms up, and that must
be retried rather than read as a contract failure. A genuinely down service or a real
contract drift must still fail, with a diagnosable message.

No live stack is required: an httpx.MockTransport replays a programmed sequence of
responses, so this runs under `make test-root` (and therefore in CI's coverage job).
"""

import httpx
import pytest

from tests.contract.test_openapi_schema import _fetch_openapi

URL = "https://localhost/api/execution/openapi.json"
VALID_BODY = '{"openapi": "3.1.0", "paths": {}, "components": {"schemas": {}}}'


def _client(responses: list[tuple[int, str]]) -> tuple[httpx.Client, list[int]]:
    """Build a client whose GETs return `responses` in order, then repeat the last one.

    Returns the client and a single-element list used as a mutable call counter.
    """
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(calls[0], len(responses) - 1)
        calls[0] += 1
        status, body = responses[idx]
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_succeeds_on_first_try():
    client, calls = _client([(200, VALID_BODY)])
    with client:
        doc = _fetch_openapi(client, URL, backoff=0)
    assert doc["openapi"] == "3.1.0"
    assert calls[0] == 1


def test_retries_then_succeeds_on_empty_200_body():
    # Two empty 200s (the observed warm-up race), then a valid document.
    client, calls = _client([(200, ""), (200, "   "), (200, VALID_BODY)])
    with client:
        doc = _fetch_openapi(client, URL, backoff=0)
    assert doc["paths"] == {}
    assert calls[0] == 3


def test_retries_on_non_200_then_succeeds():
    client, calls = _client([(503, ""), (200, VALID_BODY)])
    with client:
        doc = _fetch_openapi(client, URL, backoff=0)
    assert doc["openapi"] == "3.1.0"
    assert calls[0] == 2


def test_retries_on_garbled_json_then_succeeds():
    client, calls = _client([(200, "not json at all"), (200, VALID_BODY)])
    with client:
        doc = _fetch_openapi(client, URL, backoff=0)
    assert doc["openapi"] == "3.1.0"
    assert calls[0] == 2


def test_gives_up_on_persistent_empty_body_with_diagnostic():
    client, _ = _client([(200, "")])
    with client:
        with pytest.raises(AssertionError) as exc:
            _fetch_openapi(client, URL, attempts=3, backoff=0)
    msg = str(exc.value)
    assert "after 3 attempts" in msg
    assert "last status 200" in msg


def test_gives_up_on_persistent_5xx_reports_status_and_body():
    client, calls = _client([(500, "upstream boom")])
    with client:
        with pytest.raises(AssertionError) as exc:
            _fetch_openapi(client, URL, attempts=4, backoff=0)
    msg = str(exc.value)
    assert "after 4 attempts" in msg
    assert "last status 500" in msg
    assert "upstream boom" in msg
    assert calls[0] == 4


def test_exhausts_exact_attempt_budget():
    # A success on what would be the 4th call must never happen when budget is 3.
    client, calls = _client([(200, ""), (200, ""), (200, ""), (200, VALID_BODY)])
    with client:
        with pytest.raises(AssertionError):
            _fetch_openapi(client, URL, attempts=3, backoff=0)
    assert calls[0] == 3
