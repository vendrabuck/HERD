"""The config service bounds every request body (issue #1466, OPS-VALID-9).

Config has no Settings model and must not import herd_common, so `app/main.py`
passes `app.request_limits.max_request_body_bytes()` (`MAX_REQUEST_BODY_BYTES` from
the environment) to its standalone copy of `install_validation_errors`. It installs
the copy without the lone-surrogate check (OPS-CONFIG-8), so the limit is the
innermost middleware. These requests carry no session. A body that is not JSON
answers `json_invalid` at the limit (handed on whole and parsed) and 413 one byte
over it (never parsed); an honest settings save is refused by the session check
while one over the limit is the 413, so the limit comes first.
"""

from __future__ import annotations

import logging

import pytest
from app import request_limits
from app.main import app
from app.validation_errors import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    LimitRequestBody,
    RefuseLoneSurrogates,
    body_too_large_error,
)
from httpx import ASGITransport, AsyncClient

LIMIT = request_limits.max_request_body_bytes()
_JSON = {"content-type": "application/json"}
_BODY_ROUTES = [("POST", "/login"), ("POST", "/change-password"), ("PUT", "/settings")]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _refused(resp) -> bool:
    return resp.status_code == 413 and resp.json() == {"detail": [body_too_large_error(LIMIT)]}


def test_the_body_limit_is_installed_innermost_without_the_surrogate_check():
    classes = [m.cls for m in app.user_middleware]
    assert classes[-1] is LimitRequestBody
    assert RefuseLoneSurrogates not in classes
    assert app.user_middleware[-1].kwargs == {"max_body_bytes": LIMIT}


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
async def test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed(method, path):
    async with _client() as client:
        at = await client.request(method, path, content=b"x" * LIMIT, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (LIMIT + 1), headers=_JSON)
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert _refused(over), over.text[:300]
    assert "connection" not in over.headers  # the connection stays usable


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
async def test_a_chunked_body_over_the_limit_is_413(method, path):
    async def parts():
        for _ in range(5):
            yield b" " * (LIMIT // 4)

    async with _client() as client:
        resp = await client.request(method, path, content=parts(), headers=_JSON)
    assert _refused(resp), resp.text[:300]


async def test_without_a_session_the_limit_answers_before_the_session_check():
    async with _client() as client:
        honest = await client.put("/settings", content=b"{}", headers=_JSON)
        over = await client.put("/settings", content=b"{}" + b" " * LIMIT, headers=_JSON)
    assert honest.status_code == 401, honest.text[:300]
    assert _refused(over), over.text[:300]


# -- The environment reader ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_MAX_REQUEST_BODY_BYTES),
        ("", DEFAULT_MAX_REQUEST_BODY_BYTES),
        ("   ", DEFAULT_MAX_REQUEST_BODY_BYTES),
        ("1048576", 1048576),
        (" 2097152 ", 2097152),
        ("65536", 65536),
    ],
    ids=["unset", "empty", "blank", "one_mebibyte", "padded", "the_floor"],
)
def test_the_reader_takes_decimal_digits_at_least_the_floor(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    else:
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", raw)
    assert request_limits.max_request_body_bytes() == expected


# The same values HerdBaseSettings refuses (services/common/tests/test_base_settings.py,
# test_the_body_limit_refuses_what_config_also_refuses): config uses the default instead.
@pytest.mark.parametrize(
    "raw", ["8388608.0", "1_000_000", "1e6", "+65536", "65535", "0", "-1", "4MB", "abc"]
)
def test_a_value_the_services_refuse_is_the_default_with_a_warning(monkeypatch, caplog, raw):
    """As config's login limits do: a typo must not keep the first-startup page down."""
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", raw)
    caplog.set_level(logging.WARNING, logger="app.request_limits")
    assert request_limits.max_request_body_bytes() == DEFAULT_MAX_REQUEST_BODY_BYTES
    assert [r.levelno for r in caplog.records if r.name == "app.request_limits"] == [
        logging.WARNING
    ]


def test_the_reader_is_the_copy_of_the_shared_reading():
    """Config reads with the copy's `max_body_bytes_from_text`, which the identity test
    pins to the shared module's, so config and every other service agree."""
    from app import validation_errors

    assert request_limits.max_body_bytes_from_text is validation_errors.max_body_bytes_from_text
