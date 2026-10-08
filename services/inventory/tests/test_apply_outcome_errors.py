"""The text a failed config apply stores and returns (issue #1093).

Both apply paths (the immediate apply's 200 body and the scheduled job's row)
build their failure text through `apply_outcome.unreachable_error` and
`apply_outcome.refusal_error`, so rows and answers carry HERD-authored text,
the exception class, and the upstream status, never an exception's text or an
upstream body. The raw text goes to the log message only.
"""

import logging

import httpx
import pytest
from app.services.apply_outcome import refusal_error, unreachable_error

_SECRET_URL = "http://execution:8000/execute/internal"


def test_unreachable_error_is_the_class_name_only(caplog):
    exc = httpx.ConnectError(f"All connection attempts failed for {_SECRET_URL}")
    with caplog.at_level(logging.WARNING, logger="app.services.apply_outcome"):
        text = unreachable_error(exc)
    assert text == "execution service unreachable (ConnectError)"
    assert _SECRET_URL not in text
    assert any(_SECRET_URL in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "exc, name",
    [
        (httpx.ReadTimeout("read timed out"), "ReadTimeout"),
        (httpx.RemoteProtocolError("peer closed"), "RemoteProtocolError"),
    ],
)
def test_unreachable_error_names_each_transport_class(exc, name):
    assert unreachable_error(exc) == f"execution service unreachable ({name})"


@pytest.mark.parametrize(
    "status, body, expected",
    [
        # A structured detail keeps its operator-facing message.
        (
            409,
            {
                "detail": {
                    "error": "driver_cannot_configure",
                    "message": "This driver has no configure method.",
                }
            },
            "execution answered HTTP 409: This driver has no configure method.",
        ),
        (
            409,
            {"detail": {"error": "device_has_no_driver", "message": "No driver assigned."}},
            "execution answered HTTP 409: No driver assigned.",
        ),
        # A plain string detail is not relayed (it can carry exception text).
        (503, {"detail": f"Failed to fetch device: cannot reach {_SECRET_URL}"}, None),
        (403, {"detail": "Admin access or device manage grant required"}, None),
        # A validation list, a structured detail without a usable message, a
        # body that is not an object: status only.
        (422, {"detail": [{"loc": ["body"], "msg": "bad", "type": "x"}]}, None),
        (409, {"detail": {"error": "x", "message": ""}}, None),
        (409, {"detail": {"error": "x", "message": 7}}, None),
        (500, ["Internal", "Server", "Error"], None),
        (500, "Internal Server Error", None),
    ],
)
def test_refusal_error_carries_status_and_structured_message_only(status, body, expected):
    resp = httpx.Response(status, json=body)
    text = refusal_error(resp)
    assert text == (expected or f"execution answered HTTP {status}")
    assert _SECRET_URL not in text


def test_refusal_error_non_json_body_is_status_only_and_logged(caplog):
    resp = httpx.Response(502, text="<html>bad gateway from the proxy</html>")
    with caplog.at_level(logging.WARNING, logger="app.services.apply_outcome"):
        text = refusal_error(resp)
    assert text == "execution answered HTTP 502"
    assert any("bad gateway from the proxy" in r.getMessage() for r in caplog.records)
