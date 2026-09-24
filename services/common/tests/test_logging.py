import datetime
import json
import logging
import uuid

import pytest
from fastapi import FastAPI
from herd_common.logging import JSONFormatter, RequestLoggingMiddleware, setup_logging
from httpx import ASGITransport, AsyncClient


def _make_record(msg="test message", level=logging.INFO, **extras):
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extras.items():
        setattr(record, key, value)
    return record


def _record_with_extra(key, value):
    """Build a record and set one extra by name via setattr.

    Unlike _make_record(**{key: value}), this never risks the extra name
    colliding with _make_record's own "msg"/"level" parameters (relevant for
    an envelope-collision test that sets an extra literally named "level").
    """
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None,
    )
    setattr(record, key, value)
    return record


def test_json_formatter_basic_output():
    formatter = JSONFormatter("test-service")
    record = _make_record("hello world")
    output = json.loads(formatter.format(record))
    assert output["level"] == "INFO"
    assert output["service"] == "test-service"
    assert output["logger"] == "test.logger"
    assert output["message"] == "hello world"
    assert "timestamp" in output


def test_json_formatter_includes_extras():
    formatter = JSONFormatter("svc")
    record = _make_record(method="GET", path="/api/test", status_code=200)
    output = json.loads(formatter.format(record))
    assert output["method"] == "GET"
    assert output["path"] == "/api/test"
    assert output["status_code"] == 200


def test_json_formatter_ignores_unset_extras():
    formatter = JSONFormatter("svc")
    record = _make_record()
    output = json.loads(formatter.format(record))
    assert "method" not in output
    assert "path" not in output
    assert "status_code" not in output


def test_json_formatter_includes_exception():
    formatter = JSONFormatter("svc")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="error happened",
            args=(),
            exc_info=sys.exc_info(),
        )
    output = json.loads(formatter.format(record))
    assert "exception" in output
    assert "ValueError" in output["exception"]
    assert "boom" in output["exception"]


def test_json_formatter_emits_unlisted_extras():
    # Issue #872: any extra, not just the old eleven-key allowlist, must
    # reach the formatted line.
    formatter = JSONFormatter("svc")
    record = _make_record(iteration=3, tool_names=["a", "b"])
    output = json.loads(formatter.format(record))
    assert output["iteration"] == 3
    assert output["tool_names"] == ["a", "b"]


def test_json_formatter_old_allowlisted_keys_unchanged():
    formatter = JSONFormatter("svc")
    record = _make_record(
        method="GET",
        path="/api/test",
        status_code=200,
        duration_ms=12.5,
        user_id="u1",
        action="login",
        email="a@example.com",
        username="alice",
        role="admin",
        device_id="d1",
        reservation_id="r1",
    )
    output = json.loads(formatter.format(record))
    assert output["method"] == "GET"
    assert output["path"] == "/api/test"
    assert output["status_code"] == 200
    assert output["duration_ms"] == 12.5
    assert output["user_id"] == "u1"
    assert output["action"] == "login"
    assert output["email"] == "a@example.com"
    assert output["username"] == "alice"
    assert output["role"] == "admin"
    assert output["device_id"] == "d1"
    assert output["reservation_id"] == "r1"


@pytest.mark.parametrize("colliding_key", ["service", "level", "timestamp", "logger", "exception"])
def test_json_formatter_envelope_collision_renamed(colliding_key):
    formatter = JSONFormatter("svc")
    record = _record_with_extra(colliding_key, "attacker-supplied")
    output = json.loads(formatter.format(record))
    # The envelope value survives untouched.
    if colliding_key == "exception":
        assert "exception" not in output  # no exc_info on this record
    else:
        assert output[colliding_key] != "attacker-supplied"
    # The colliding extra is renamed rather than dropped or overwriting.
    assert output[f"extra_{colliding_key}"] == "attacker-supplied"


def test_json_formatter_envelope_collision_exception_with_real_exc_info():
    import sys

    formatter = JSONFormatter("svc")
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="error happened",
            args=(),
            exc_info=sys.exc_info(),
        )
    record.exception = "attacker-supplied"
    output = json.loads(formatter.format(record))
    assert "ValueError" in output["exception"]
    assert output["extra_exception"] == "attacker-supplied"


REDACTED_KEYS = [
    "password",
    "access_token",
    "internal_token",
    "Authorization",
    "client_secret",
    "api_key",
    "AI_API_KEY",
    "kek",
    "session_cookie",
    "credentials",
]


@pytest.mark.parametrize("key", REDACTED_KEYS)
def test_json_formatter_redacts_credential_shaped_keys(key):
    formatter = JSONFormatter("svc")
    secret_value = "sekrit-value-do-not-leak-8f2a"
    record = _make_record(**{key: secret_value})
    line = formatter.format(record)
    assert secret_value not in line
    output = json.loads(line)
    assert output[key] == "[redacted]"


NON_REDACTED_KEYS = {
    # ai_client token metering (#848/#872): counts, not secrets.
    "input_tokens": 120,
    "output_tokens": 45,
    # a database row id, not the token value itself.
    "token_id": "tok-1",
    # same counting shape as input_tokens/output_tokens.
    "token_count": 3,
    "error": "boom",
    "reason": "not_found",
    "device_id": "d1",
}


@pytest.mark.parametrize("key,value", list(NON_REDACTED_KEYS.items()))
def test_json_formatter_does_not_redact_lookalike_keys(key, value):
    formatter = JSONFormatter("svc")
    record = _make_record(**{key: value})
    output = json.loads(formatter.format(record))
    assert output[key] == value


def test_json_formatter_omits_reserved_attributes():
    formatter = JSONFormatter("svc")
    record = _make_record("hello")
    output = json.loads(formatter.format(record))
    for reserved in ("args", "msg", "levelno", "pathname", "taskName", "funcName", "process"):
        assert reserved not in output


def test_json_formatter_omits_none_extra():
    formatter = JSONFormatter("svc")
    record = _make_record(some_extra=None)
    output = json.loads(formatter.format(record))
    assert "some_extra" not in output


def test_json_formatter_serializes_uuid_datetime_and_raising_object():
    class Raises:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    formatter = JSONFormatter("svc")
    record = _make_record(
        a_uuid=uuid.uuid4(),
        a_datetime=datetime.datetime(2026, 9, 24, 12, 0, 0),
        a_bad_object=Raises(),
        a_fine_value="still here",
    )
    output = json.loads(formatter.format(record))
    assert isinstance(output["a_uuid"], str)
    assert isinstance(output["a_datetime"], str)
    assert output["a_bad_object"] == "<unserializable>"
    assert output["a_fine_value"] == "still here"


def test_setup_logging_configures_root_logger():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_noisy_levels = {
        name: logging.getLogger(name).level
        for name in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine")
    }
    try:
        setup_logging("test-svc")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JSONFormatter)
        # Noisy loggers suppressed
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
    finally:
        # setup_logging replaces the root logger's handlers wholesale (issue
        # #534 test-isolation follow-up): restore them so later tests in this
        # process see the same root logger they would have without this test
        # running (in particular, so pytest's own log-capture handler is not
        # left removed for the rest of the session).
        root.handlers.clear()
        root.handlers.extend(original_handlers)
        root.setLevel(original_level)
        for name, level in original_noisy_levels.items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.asyncio
async def test_request_logging_middleware_emits_access_record(caplog):
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/ping")
    async def ping():
        return {"status": "ok"}

    caplog.set_level(logging.INFO, logger="herd.access")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ping")
    assert response.status_code == 200

    records = [r for r in caplog.records if r.name == "herd.access"]
    assert records, "expected an access log record"
    record = records[-1]
    assert record.method == "GET"
    assert record.path == "/ping"
    assert record.status_code == 200
    assert isinstance(record.duration_ms, float)
