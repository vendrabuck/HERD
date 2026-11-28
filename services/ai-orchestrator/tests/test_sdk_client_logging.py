"""Quieter request logging for the SDK client (OPS-LOG-8).

The Anthropic and OpenAI SDKs send through `httpx2`, a fork of httpx that logs its
per-request line under its own logger name. `herd_common.logging.setup_logging`
holds that logger at WARNING as it does `httpx`'s; this drives a real `httpx2`
call through a mock transport after setup_logging and reads what the service's
JSON handler wrote.
"""

import io
import json
import logging

import httpx2
import pytest
from herd_common.logging import HTTP_TRANSPORT_LOGGERS, QUIET_LOGGERS, setup_logging

_URL = "https://sdk-user:Z4mq-value@provider.test/v1/messages?beta=1"


@pytest.fixture
def service_log():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_levels = {
        name: logging.getLogger(name).level for name in QUIET_LOGGERS + HTTP_TRANSPORT_LOGGERS
    }
    buffer = io.StringIO()

    def _setup(level: str) -> io.StringIO:
        setup_logging("ai-orchestrator", level=level)
        root.handlers[0].setStream(buffer)
        return buffer

    try:
        yield _setup
    finally:
        root.handlers.clear()
        root.handlers.extend(original_handlers)
        root.setLevel(original_level)
        for name, level in original_levels.items():
            logging.getLogger(name).setLevel(level)


def _answer(request):
    return httpx2.Response(200, json={"ok": True}, extensions={"reason_phrase": b"Odd Reason"})


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
async def test_an_sdk_client_request_writes_no_client_line_to_the_service_log(service_log, level):
    buffer = service_log(level)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_answer)) as client:
        response = await client.post(_URL, json={})
    logging.getLogger("herd.test").info("after the call")

    assert response.status_code == 200
    text = buffer.getvalue()
    assert [json.loads(line)["message"] for line in text.splitlines()] == ["after the call"]
    for fragment in ("sdk-user", "Z4mq-value", "provider.test", "HTTP Request", "Odd Reason"):
        assert fragment not in text, fragment


def test_the_sdk_client_line_is_what_the_quiet_level_holds_back(service_log):
    """The precondition: with its logger at INFO the same client writes the line."""
    buffer = service_log("INFO")
    logging.getLogger("httpx2").setLevel(logging.INFO)

    with httpx2.Client(transport=httpx2.MockTransport(_answer)) as client:
        client.post(_URL, json={})

    assert "HTTP Request: POST" in buffer.getvalue()
    assert "provider.test" in buffer.getvalue()
