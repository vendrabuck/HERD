import json
import logging

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
