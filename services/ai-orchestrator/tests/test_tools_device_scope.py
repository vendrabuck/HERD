"""Device scope of the assistant tools (issue #1054).

Every device-id argument must name one of the reservation's own devices. The
check runs in dispatch(), before any handler, for read and write tools alike,
and fails closed when the reservation's device list cannot be read. These
tests use the real device-list read against an httpx.MockTransport.
"""

import json
import logging
import uuid

import httpx
import pytest
from app.config import settings
from app.services.tools import (
    DEVICE_ID_ARGUMENTS,
    DEVICE_SET_UNAVAILABLE_ERROR,
    DOCS_TOOL_DEFINITIONS,
    TOOL_DEFINITIONS,
    WRITE_TOOL_DEFINITIONS,
    ToolDispatcher,
)
from herd_common.logging import JSONFormatter

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
IN_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
IN_B = uuid.UUID("44444444-4444-4444-4444-444444444444")
OUTSIDE = uuid.UUID("99999999-9999-9999-9999-999999999999")
VERSION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TOKEN = "scope-token"

RESERVATION_URL = f"{settings.reservations_service_url.rstrip('/')}/{RESERVATION_ID}"


@pytest.fixture(autouse=True)
def _write_tools_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ai_write_tools_enabled", True)


def _is_reservation_read(request: httpx.Request) -> bool:
    return request.method == "GET" and str(request.url) == RESERVATION_URL


class _Recorder:
    """MockTransport handler: answers the reservation read with `reservation`
    and every other request with a 599 the tests never expect to reach."""

    def __init__(self, reservation=None, *, status=200, raise_exc=None):
        self.requests: list[httpx.Request] = []
        self._reservation = (
            {"id": str(RESERVATION_ID), "device_ids": [str(IN_A), str(IN_B)]}
            if reservation is None
            else reservation
        )
        self._status = status
        self._raise = raise_exc

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if _is_reservation_read(request):
            if self._raise is not None:
                raise self._raise
            if isinstance(self._reservation, (dict, list)):
                return httpx.Response(self._status, json=self._reservation)
            return httpx.Response(self._status, text=self._reservation)
        return httpx.Response(599, json={"detail": "downstream reached"})

    @property
    def downstream(self) -> list[httpx.Request]:
        return [r for r in self.requests if not _is_reservation_read(r)]

    @property
    def reservation_reads(self) -> list[httpx.Request]:
        return [r for r in self.requests if _is_reservation_read(r)]


def _dispatcher(recorder: _Recorder) -> ToolDispatcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client)


def _args(tool: str, device: uuid.UUID) -> dict:
    """Minimal valid arguments for a tool, every device argument set to `device`."""
    extra = {
        "propose_config_change": {"config_payload": {"commands": []}, "description": "d"},
        "schedule_config_apply": {"version_id": VERSION_ID},
    }.get(tool, {})
    return {**{key: str(device) for key in DEVICE_ID_ARGUMENTS[tool]}, **extra}


# --- The table covers every device argument ------------------------------


def test_every_device_id_property_is_scoped():
    """A tool that gains a *device_id property must be added to
    DEVICE_ID_ARGUMENTS, or its argument would skip the scope check."""
    every = TOOL_DEFINITIONS + WRITE_TOOL_DEFINITIONS + DOCS_TOOL_DEFINITIONS
    found = {
        d["name"]: tuple(k for k in d["input_schema"]["properties"] if k.endswith("device_id"))
        for d in every
    }
    found = {name: keys for name, keys in found.items() if keys}
    assert found == DEVICE_ID_ARGUMENTS


# --- Refusal: outside the reservation ------------------------------------


@pytest.mark.parametrize("tool", sorted(DEVICE_ID_ARGUMENTS))
async def test_a_device_outside_the_reservation_is_refused_before_any_downstream_call(tool):
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        result = await d.dispatch(tool, _args(tool, OUTSIDE))

    first_key = DEVICE_ID_ARGUMENTS[tool][0]
    assert result["is_error"] is True
    assert json.loads(result["content"]) == {
        "is_error": True,
        "message": f"{first_key} is not a device of this reservation",
    }
    assert recorder.downstream == []
    assert d.call_log[-1].error == f"{first_key} is not a device of this reservation"
    assert d.side_effects == []


@pytest.mark.parametrize("tool", sorted(DEVICE_ID_ARGUMENTS))
async def test_a_device_of_the_reservation_reaches_the_handler(tool):
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        await d.dispatch(tool, _args(tool, IN_A))

    # The handler ran: its first downstream call went out (and met the 599).
    assert recorder.downstream, f"{tool} never reached its handler"
    assert "is not a device of this reservation" not in (d.call_log[-1].error or "")


async def test_find_path_refuses_when_only_the_target_is_outside():
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        result = await d.dispatch(
            "find_path", {"source_device_id": str(IN_A), "target_device_id": str(OUTSIDE)}
        )

    assert json.loads(result["content"])["message"] == (
        "target_device_id is not a device of this reservation"
    )
    assert recorder.downstream == []


async def test_find_path_between_two_reservation_devices_is_allowed():
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        await d.dispatch(
            "find_path", {"source_device_id": str(IN_A), "target_device_id": str(IN_B)}
        )

    assert [r.url.path for r in recorder.downstream] == ["/pathfind"]


async def test_write_tool_gate_still_answers_first_when_write_tools_are_off(monkeypatch):
    monkeypatch.setattr(settings, "ai_write_tools_enabled", False)
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        result = await d.dispatch("propose_config_change", _args("propose_config_change", OUTSIDE))

    assert json.loads(result["content"])["message"] == "write tools are disabled"
    assert recorder.requests == []


# --- Calls with no device argument ---------------------------------------


async def test_list_executions_without_a_device_filter_reads_no_device_list():
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        await d.dispatch("list_executions_for_reservation", {})

    assert recorder.reservation_reads == []
    assert [r.url.path for r in recorder.downstream] == ["/runs"]


async def test_a_missing_device_argument_is_still_refused_by_the_handler():
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        result = await d.dispatch("get_device", {})

    assert json.loads(result["content"])["message"] == "missing required argument: device_id"
    assert recorder.requests == []


async def test_a_malformed_device_id_is_refused_without_any_http_call():
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        result = await d.dispatch("get_device", {"device_id": "not-a-uuid"})

    assert json.loads(result["content"])["message"] == "device_id must be a UUID; got 'not-a-uuid'"
    assert recorder.requests == []


# --- The device-list read ------------------------------------------------


async def test_the_device_list_is_read_with_the_callers_jwt_once_per_turn():
    recorder = _Recorder()
    async with _dispatcher(recorder) as d:
        await d.dispatch("get_device", {"device_id": str(IN_A)})
        await d.dispatch("get_device_ports", {"device_id": str(IN_B)})
        await d.dispatch("get_device", {"device_id": str(OUTSIDE)})

    assert len(recorder.reservation_reads) == 1
    assert recorder.reservation_reads[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("recorder", "why"),
    [
        (_Recorder(status=404, reservation={"detail": "Reservation not found"}), "404"),
        (_Recorder(status=500, reservation={"detail": "boom"}), "500"),
        (_Recorder(raise_exc=httpx.ConnectError("refused")), "transport error"),
        (_Recorder(reservation={"id": str(RESERVATION_ID)}), "no device_ids key"),
        (_Recorder(reservation={"device_ids": "not-a-list"}), "device_ids not a list"),
        (_Recorder(reservation={"device_ids": ["not-a-uuid"]}), "malformed id"),
        (_Recorder(reservation=[str(IN_A)]), "body not an object"),
        (_Recorder(reservation="<html>not json</html>"), "body not JSON"),
    ],
)
async def test_an_unreadable_device_list_fails_closed(recorder, why):
    async with _dispatcher(recorder) as d:
        result = await d.dispatch("get_device", {"device_id": str(IN_A)})

    assert result["is_error"] is True, why
    assert json.loads(result["content"])["message"] == DEVICE_SET_UNAVAILABLE_ERROR, why
    assert DEVICE_SET_UNAVAILABLE_ERROR == "the reservation's device list could not be read"
    assert recorder.downstream == [], why


async def test_a_failed_device_list_read_is_retried_on_the_next_call():
    answers = iter(
        [
            httpx.Response(503, json={"detail": "busy"}),
            httpx.Response(200, json={"device_ids": [str(IN_A)]}),
        ]
    )
    downstream: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_reservation_read(request):
            return next(answers)
        downstream.append(request)
        return httpx.Response(599)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        first = await d.dispatch("get_device", {"device_id": str(IN_A)})
        await d.dispatch("get_device", {"device_id": str(IN_A)})

    assert json.loads(first["content"])["message"] == DEVICE_SET_UNAVAILABLE_ERROR
    assert len(downstream) == 1


# --- Logs ----------------------------------------------------------------


async def test_a_refusal_is_logged_by_tool_and_argument_only(caplog):
    recorder = _Recorder()
    with caplog.at_level(logging.WARNING, logger="app.services.tools"):
        async with _dispatcher(recorder) as d:
            await d.dispatch("propose_config_change", _args("propose_config_change", OUTSIDE))

    records = [r for r in caplog.records if r.getMessage() == "ai_tool_device_outside_reservation"]
    assert len(records) == 1
    formatted = JSONFormatter("ai-orchestrator").format(records[0])
    body = json.loads(formatted)
    assert body["tool"] == "propose_config_change"
    assert body["argument"] == "device_id"
    assert str(OUTSIDE) not in formatted


async def test_an_unreadable_device_list_is_logged_by_class_and_status(caplog):
    recorder = _Recorder(status=500, reservation={"detail": "internal detail text"})
    with caplog.at_level(logging.WARNING, logger="app.services.tools"):
        async with _dispatcher(recorder) as d:
            await d.dispatch("get_device", {"device_id": str(IN_A)})

    records = [r for r in caplog.records if r.getMessage() == "ai_tool_device_scope_unavailable"]
    assert len(records) == 1
    formatted = JSONFormatter("ai-orchestrator").format(records[0])
    body = json.loads(formatted)
    assert body["status_code"] == 500
    assert body["error_class"] == "ValueError"
    assert "internal detail text" not in formatted
