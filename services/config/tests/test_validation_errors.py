"""The config service's request-validation (422) handler.

FastAPI's default handler renders every pydantic error with its `input`: for a
login or change-password body that is the submitted password, and for a missing
field it is the whole body, password included. Starlette cannot encode a lone
surrogate, so a surrogate in that echoed input turned the 422 into a 500. The
config service renders each error as exactly `{type, loc, msg}` and replaces any
lone surrogate in `loc` or `msg` with U+FFFD (a standalone copy of the shared
`herd_common.validation_errors`, first written for auth in issue #1426; config
imports neither herd_common nor auth). The last test pins the copy's code identical
to the shared module's, so the hand-kept copy cannot drift.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from app.main import app
from app.validation_errors import (
    RENDERED_KEYS,
    restrict_validation_error_schema,
    validation_error_handler,
)
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

PW = "pw-Secret-9z"
SUR = chr(0xD800)  # a lone high surrogate, built so no source escape can be decoded early
LOW = chr(0xDFFF)
NUMBER = 98765432
RENDERED = {"type", "loc", "msg"}


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _raw(value) -> bytes:
    """A JSON body with every non-ASCII code point escaped, so a lone surrogate
    reaches the route exactly as written."""
    return json.dumps(value, ensure_ascii=True).encode("ascii")


async def _session(client) -> dict:
    resp = await client.post("/login", json={"password": "test-config-pass"})
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _forms(secret: str) -> list[bytes]:
    """Every way `secret` could appear in a response body."""
    return [
        secret.encode("utf-8", "surrogatepass"),
        json.dumps(secret, ensure_ascii=True)[1:-1].encode("ascii"),
        json.dumps(secret, ensure_ascii=False)[1:-1].encode("utf-8", "surrogatepass"),
    ]


def _assert_clean(resp, secrets: list[str]) -> None:
    for secret in secrets:
        for form in _forms(secret):
            assert form not in resp.content, (secret, resp.content[:200])
    if resp.status_code == 422 and isinstance(resp.json()["detail"], list):
        assert [set(e) for e in resp.json()["detail"]] == [RENDERED] * len(resp.json()["detail"])


def _password_cases(field: str) -> dict[str, tuple[bytes, list[str]]]:
    """Each way a password-bearing field can fail, with the secrets that must not
    appear in the answer."""
    return {
        "missing_typo_key": (_raw({field[:-1]: PW}), [PW]),
        "number": (_raw({field: NUMBER}), [str(NUMBER)]),
        "list": (_raw({field: [PW]}), [PW]),
        "object_keyed_by_password": (_raw({field: {PW: 1}}), [PW]),
        "object_valued_by_password": (_raw({field: {"k": PW}}), [PW]),
        "surrogate_in_list": (_raw({field: [SUR + PW]}), [SUR + PW, PW]),
        "surrogate_under_typo_key": (_raw({"np": SUR + PW}), [SUR + PW, PW]),
        "low_surrogate_key": (_raw({LOW: PW}), [PW]),
        "extra_key": (_raw({field: "wrong-pass-1", "extra": PW}), [PW]),
        "body_is_the_password": (_raw(PW), [PW]),
        "body_is_a_list": (_raw([PW]), [PW]),
        "malformed_json": (b'{"' + field.encode() + b'": "' + PW.encode(), [PW]),
    }


_LOGIN = _password_cases("password")
_CHANGE = _password_cases("new_password")
_SETTINGS = {
    "values_is_the_secret": (_raw({"values": PW}), [PW]),
    "values_is_a_list": (_raw({"values": [PW]}), [PW]),
    "values_missing": (_raw({"valuez": {"POSTGRES_PASSWORD": PW}}), [PW]),
    "values_surrogate": (_raw({"values": SUR + PW}), [SUR + PW, PW]),
    "body_is_the_secret": (_raw(PW), [PW]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "secrets"), _LOGIN.values(), ids=_LOGIN.keys())
async def test_login_never_echoes_the_password(body, secrets, async_client):
    resp = await async_client.post(
        "/login", content=body, headers={"content-type": "application/json"}
    )
    assert resp.status_code in (401, 422)
    _assert_clean(resp, secrets)


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "secrets"), _CHANGE.values(), ids=_CHANGE.keys())
async def test_change_password_never_echoes_the_password(body, secrets, async_client):
    headers = await _session(async_client)
    resp = await async_client.post(
        "/change-password",
        content=body,
        headers={"content-type": "application/json", **headers},
    )
    # The extra key is ignored, so that body changes the password (200).
    assert resp.status_code in (200, 422)
    _assert_clean(resp, secrets)


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "secrets"), _SETTINGS.values(), ids=_SETTINGS.keys())
async def test_save_settings_never_echoes_a_secret_in_a_malformed_body(body, secrets, async_client):
    headers = await _session(async_client)
    resp = await async_client.put(
        "/settings", content=body, headers={"content-type": "application/json", **headers}
    )
    assert resp.status_code == 422
    _assert_clean(resp, secrets)


# The six inputs the review found, exactly: two echoes and four 500s.
_REVIEW_INPUTS = {
    "change_number": ("/change-password", _raw({"new_password": NUMBER}), str(NUMBER)),
    "change_typo_key": ("/change-password", _raw({"new_pasword": PW}), PW),
    "change_list_surrogate": ("/change-password", _raw({"new_password": ["abc" + SUR]}), "abc"),
    "change_np_surrogate": ("/change-password", _raw({"np": "abcdefg" + SUR}), "abcdefg"),
    "login_list_surrogate": ("/login", _raw({"password": [SUR]}), SUR),
    "login_pw_surrogate": ("/login", _raw({"pw": SUR}), SUR),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "secret"), _REVIEW_INPUTS.values(), ids=_REVIEW_INPUTS.keys()
)
async def test_the_review_inputs_are_a_clean_422(path, body, secret, async_client):
    headers = await _session(async_client) if path == "/change-password" else {}
    resp = await async_client.post(
        path, content=body, headers={"content-type": "application/json", **headers}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    _assert_clean(resp, [secret])


# -- The handler itself (mirrors herd_common's tests) -------------------------------


REPLACEMENT = chr(0xFFFD)


async def _render(errors: list[dict]) -> tuple[int, object]:
    response = await validation_error_handler(None, RequestValidationError(errors))
    return response.status_code, json.loads(response.body)


def test_the_handler_is_installed_for_request_validation_errors():
    assert app.exception_handlers[RequestValidationError] is validation_error_handler


@pytest.mark.asyncio
async def test_the_handler_keeps_type_loc_msg_and_drops_input_ctx_and_everything_else():
    status, body = await _render(
        [
            {
                "type": "string_type",
                "loc": ("body", "new_password"),
                "msg": "Input should be a valid string",
                "input": [PW],
                "ctx": {"error": PW},
                "url": "https://errors.pydantic.dev/",
            },
            {
                "type": "missing",
                "loc": ("body", "password"),
                "msg": "Field required",
                "input": {"pasword": PW},
            },
        ]
    )
    assert status == 422
    assert body == {
        "detail": [
            {
                "type": "string_type",
                "loc": ["body", "new_password"],
                "msg": "Input should be a valid string",
            },
            {"type": "missing", "loc": ["body", "password"], "msg": "Field required"},
        ]
    }


@pytest.mark.asyncio
async def test_the_handler_replaces_a_lone_surrogate_in_loc_and_msg_and_keeps_integers():
    """Defensive: no config route is known to put a surrogate in `loc` or `msg`,
    but a 500 is the cost of being wrong, so the handler never assumes it."""
    status, body = await _render(
        [
            {
                "type": "value_error",
                "loc": ("body", "a" + SUR + "b", 3),
                "msg": "found `" + LOW + "` at 1",
                "input": SUR,
            }
        ]
    )
    assert status == 422
    assert body == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "a" + REPLACEMENT + "b", 3],
                "msg": "found `" + REPLACEMENT + "` at 1",
            }
        ]
    }


@pytest.mark.asyncio
async def test_the_handler_keeps_honest_non_ascii_text_unchanged():
    text = "Caf" + chr(0xE9) + " " + chr(0x1F511)
    _, body = await _render([{"type": "value_error", "loc": ("body", "x"), "msg": text}])
    assert body["detail"][0]["msg"] == text


def test_the_openapi_document_advertises_exactly_the_rendered_keys():
    app.openapi_schema = None  # rebuild through the override, not a cached copy
    component = app.openapi()["components"]["schemas"]["ValidationError"]
    assert set(component["properties"]) == set(RENDERED_KEYS) == RENDERED
    assert sorted(component["required"]) == ["loc", "msg", "type"]
    assert app.openapi() is app.openapi()  # built once, then cached


@pytest.mark.asyncio
async def test_every_rendered_error_has_exactly_the_advertised_keys():
    advertised = set(app.openapi()["components"]["schemas"]["ValidationError"]["properties"])
    _, body = await _render(
        [{"type": "missing", "loc": ("body", "x"), "msg": "Field required", "input": {}}]
    )
    assert [set(error) for error in body["detail"]] == [advertised]


def test_restrict_leaves_a_document_without_the_component_alone():
    document = {"openapi": "3.1.0", "paths": {}}
    assert restrict_validation_error_schema(document) == {"openapi": "3.1.0", "paths": {}}


# -- The copy is the shared module's code -------------------------------------------


def _code(path: Path) -> str:
    """The module's AST dump with every docstring removed: what the module does,
    never how its prose reads."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def test_the_code_is_identical_to_the_shared_module():
    """app/validation_errors.py is a copy of herd_common.validation_errors (the config
    image has no herd_common). Its code, every statement but the docstrings, must
    equal the shared module's, so a change made to one copy alone fails here. A
    source-level comparison was chosen over comparing outputs on a corpus: it sees
    every difference, including one no corpus input reaches. On the host both are
    importable."""
    import herd_common.validation_errors as shared
    from app import validation_errors as copy

    assert _code(Path(copy.__file__)) == _code(Path(shared.__file__))
    assert copy.RENDERED_KEYS == shared.RENDERED_KEYS


def test_the_code_comparison_sees_a_one_character_change(tmp_path):
    """The comparison is not vacuous: a changed regex, an extra key, or a changed
    status differs, while a reworded docstring or comment does not."""
    import herd_common.validation_errors as shared

    source = Path(shared.__file__).read_text(encoding="utf-8")
    original = tmp_path / "original.py"
    original.write_text(source, encoding="utf-8")
    reworded = tmp_path / "reworded.py"
    reworded.write_text(
        source.replace("Each error as", "Every error as").replace(
            "# The keys every", "# Each of the keys"
        ),
        encoding="utf-8",
    )
    assert _code(reworded) == _code(original)
    for old, new in [
        ('"[\\ud800-\\udfff]"', '"[\\ud800-\\udbff]"'),
        ('("type", "loc", "msg")', '("type", "loc", "msg", "input")'),
        ("HTTP_422_UNPROCESSABLE_CONTENT", "HTTP_400_BAD_REQUEST"),
    ]:
        assert old in source, old
        changed = tmp_path / "changed.py"
        changed.write_text(source.replace(old, new), encoding="utf-8")
        assert _code(changed) != _code(original), old
