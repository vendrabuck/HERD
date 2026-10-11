"""Unit tests for the strict device teardown helpers (issues #940 and #1158).

`tests/integration/_device_teardown.py` backs the integration fixtures and tests
that delete devices and ports. The stack is faked with `httpx.MockTransport`, so
these run with no stack: they pin the cleanup of leftover cables, the bounded wait
on a live reservation, and that every other refusal fails loudly with the response
body; for `delete_devices_checked`, that every device is attempted, that the
in-use wait is one budget for the batch, and that one error names every refused
device; and for `delete_port_checked`, that only 204 and 404 pass.
"""

import json

import httpx
import pytest

from tests.integration import _device_teardown as td

DEVICE = "d1"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(td, "IN_USE_POLL_SECONDS", 0.0)


class FakeStack:
    """Scripted inventory plus a mutable connection table for one device."""

    def __init__(self, delete_script, connections=()):
        self.delete_script = list(delete_script)
        self.connections = list(connections)
        self.device_deletes = 0
        self.conn_deletes: list[str] = []
        self.conn_delete_status = 204

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "DELETE" and path == f"/inventory/devices/{DEVICE}":
            self.device_deletes += 1
            status, detail = (
                self.delete_script.pop(0)
                if len(self.delete_script) > 1
                else (self.delete_script[0])
            )
            if status == 204:
                return httpx.Response(204)
            return httpx.Response(status, json={"detail": detail})
        if request.method == "GET" and path == "/cabling/connections":
            assert request.url.params["device_id"] == DEVICE
            return httpx.Response(200, json={"items": [{"id": c} for c in self.connections]})
        if request.method == "DELETE" and path.startswith("/cabling/connections/"):
            cid = path.rsplit("/", 1)[1]
            self.conn_deletes.append(cid)
            if self.conn_delete_status == 204 and cid in self.connections:
                self.connections.remove(cid)
            return httpx.Response(self.conn_delete_status)
        raise AssertionError(f"unexpected {request.method} {path}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url="http://stack"
        )


CABLED = {"error": "device_cabled", "connection_count": 2, "connection_ids": ["c1", "c2"]}
IN_USE = {"error": "device_in_use", "reservation_ids": ["r"], "transit_reservation_ids": []}


@pytest.mark.asyncio
async def test_clean_delete_makes_one_call():
    stack = FakeStack([(204, None)])
    async with stack.client() as client:
        await td.delete_device_checked(client, DEVICE)
    assert stack.device_deletes == 1
    assert stack.conn_deletes == []


@pytest.mark.asyncio
async def test_already_gone_is_fine():
    stack = FakeStack([(404, "Device not found")])
    async with stack.client() as client:
        await td.delete_device_checked(client, DEVICE)


@pytest.mark.asyncio
async def test_leftover_cables_are_removed_then_delete_retried():
    stack = FakeStack([(409, CABLED), (204, None)], connections=["c1", "c2"])
    async with stack.client() as client:
        await td.delete_device_checked(client, DEVICE)
    assert sorted(stack.conn_deletes) == ["c1", "c2"]
    assert stack.device_deletes == 2


@pytest.mark.asyncio
async def test_cable_that_cannot_be_removed_fails_loudly_with_body():
    stack = FakeStack([(409, CABLED)], connections=["c1"])
    stack.conn_delete_status = 409
    async with stack.client() as client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_device_checked(client, DEVICE)
    assert "c1" in str(ei.value)
    assert "409" in str(ei.value)


@pytest.mark.asyncio
async def test_cabled_that_persists_after_cleanup_fails_loudly_with_body():
    stack = FakeStack([(409, CABLED)], connections=[])
    async with stack.client() as client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_device_checked(client, DEVICE)
    assert json.dumps(CABLED)[:20] in str(ei.value) or "device_cabled" in str(ei.value)
    assert stack.device_deletes == td.MAX_CABLE_CLEANUP_ROUNDS + 1


@pytest.mark.asyncio
async def test_in_use_is_retried_until_the_reservation_teardown_finishes():
    stack = FakeStack([(409, IN_USE), (409, IN_USE), (204, None)])
    async with stack.client() as client:
        await td.delete_device_checked(client, DEVICE, in_use_wait_seconds=5.0)
    assert stack.device_deletes == 3


@pytest.mark.asyncio
async def test_in_use_outlasting_the_window_raises_by_default():
    """The default is strict: a device a live reservation still holds at
    teardown fails the test with the refusal body."""
    assert td.STRICT_IN_USE is True
    stack = FakeStack([(409, IN_USE)])
    async with stack.client() as client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_device_checked(client, DEVICE, in_use_wait_seconds=0.0)
    assert "device_in_use" in str(ei.value)


@pytest.mark.asyncio
async def test_in_use_outlasting_the_window_is_reported_not_raised_when_not_strict(capsys):
    stack = FakeStack([(409, IN_USE)])
    async with stack.client() as client:
        await td.delete_device_checked(client, DEVICE, in_use_wait_seconds=0.0, strict_in_use=False)
    assert "device_in_use" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500, 503])
async def test_any_other_refusal_fails_loudly(status):
    stack = FakeStack([(status, "nope")])
    async with stack.client() as client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_device_checked(client, DEVICE, in_use_wait_seconds=0.0)
    assert str(status) in str(ei.value)
    assert "nope" in str(ei.value)


# --- delete_devices_checked and delete_port_checked (issue #1158) -------------------


class ManyDevices:
    """Scripted inventory for several devices; the last scripted answer repeats."""

    def __init__(self, scripts: dict, connections: dict | None = None):
        self.scripts = {device: list(script) for device, script in scripts.items()}
        self.connections = {d: list(c) for d, c in (connections or {}).items()}
        self.attempts: dict[str, int] = dict.fromkeys(scripts, 0)
        self.conn_deletes: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "DELETE" and path.startswith("/inventory/devices/"):
            device = path.rsplit("/", 1)[1]
            self.attempts[device] += 1
            script = self.scripts[device]
            status, detail = script.pop(0) if len(script) > 1 else script[0]
            if status == 204:
                return httpx.Response(204)
            return httpx.Response(status, json={"detail": detail})
        if request.method == "GET" and path == "/cabling/connections":
            device = request.url.params["device_id"]
            items = [{"id": c} for c in self.connections.get(device, [])]
            return httpx.Response(200, json={"items": items})
        if request.method == "DELETE" and path.startswith("/cabling/connections/"):
            cid = path.rsplit("/", 1)[1]
            self.conn_deletes.append(cid)
            for cables in self.connections.values():
                if cid in cables:
                    cables.remove(cid)
            return httpx.Response(204)
        raise AssertionError(f"unexpected {request.method} {path}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), base_url="http://stack"
        )


class FakeClock:
    """Stands in for the helper module's `time` and `asyncio`: sleeping moves the
    clock and takes no wall time, so the in-use budget is counted exactly."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(td, "time", fake)
    monkeypatch.setattr(td, "asyncio", fake)
    monkeypatch.setattr(td, "IN_USE_POLL_SECONDS", 0.5)
    return fake


@pytest.mark.asyncio
async def test_batch_all_clean_makes_one_call_per_device():
    stack = ManyDevices({"d1": [(204, None)], "d2": [(204, None)], "d3": [(404, "gone")]})
    async with stack.client() as client:
        await td.delete_devices_checked(client, ["d1", "d2", "d3"])
    assert stack.attempts == {"d1": 1, "d2": 1, "d3": 1}
    assert stack.conn_deletes == []


@pytest.mark.asyncio
async def test_batch_with_no_devices_is_a_no_op():
    stack = ManyDevices({})
    async with stack.client() as client:
        await td.delete_devices_checked(client, [])


@pytest.mark.asyncio
async def test_batch_attempts_every_device_after_an_early_refusal():
    """A refusal on the first device must not leave the later ones undeleted."""
    stack = ManyDevices({"d1": [(403, "nope")], "d2": [(204, None)], "d3": [(204, None)]})
    async with stack.client() as client:
        with pytest.raises(td.DeviceTeardownError):
            await td.delete_devices_checked(client, ["d1", "d2", "d3"])
    assert stack.attempts == {"d1": 1, "d2": 1, "d3": 1}


@pytest.mark.asyncio
async def test_batch_raises_one_error_naming_every_refused_device_and_only_those():
    stack = ManyDevices(
        {"d1": [(403, "forbidden-one")], "d2": [(204, None)], "d3": [(500, "broken-three")]}
    )
    async with stack.client() as client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_devices_checked(client, ["d1", "d2", "d3"])
    message = str(ei.value)
    parts = message.split("; ")
    assert len(parts) == 2, message
    assert parts[0].startswith("d1: ") and "403" in parts[0] and "forbidden-one" in parts[0]
    assert parts[1].startswith("d3: ") and "500" in parts[1] and "broken-three" in parts[1]
    assert "d2" not in message


@pytest.mark.asyncio
async def test_batch_clears_a_leftover_cable_and_retries_that_device():
    stack = ManyDevices(
        {"d1": [(409, CABLED), (204, None)], "d2": [(204, None)]},
        connections={"d1": ["c1", "c2"]},
    )
    async with stack.client() as client:
        await td.delete_devices_checked(client, ["d1", "d2"])
    assert sorted(stack.conn_deletes) == ["c1", "c2"]
    assert stack.attempts == {"d1": 2, "d2": 1}


@pytest.mark.asyncio
async def test_batch_in_use_wait_is_one_budget_for_all_devices(clock):
    """Two devices a live reservation still holds cost ONE in-use wait, not two.

    The first device uses the whole budget (a delete every poll interval until the
    deadline); the second gets a single attempt, because nothing is left to wait
    with. A per-device budget would retry the second as often as the first and
    double the teardown time under the integration suite's 30 s cap."""
    stack = ManyDevices({"d1": [(409, IN_USE)], "d2": [(409, IN_USE)], "d3": [(204, None)]})
    async with stack.client() as client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_devices_checked(client, ["d1", "d2", "d3"], in_use_wait_seconds=6.0)
    # 6.0 s of budget at 0.5 s per poll: attempts at 0.0, 0.5, ... 6.0.
    assert stack.attempts == {"d1": 13, "d2": 1, "d3": 1}
    assert clock.now == 6.0
    message = str(ei.value)
    assert "d1: " in message and "d2: " in message and "d3" not in message
    assert message.count("device_in_use") == 2


@pytest.mark.asyncio
async def test_batch_in_use_that_clears_inside_the_budget_is_not_an_error(clock):
    stack = ManyDevices({"d1": [(409, IN_USE), (409, IN_USE), (204, None)], "d2": [(204, None)]})
    async with stack.client() as client:
        await td.delete_devices_checked(client, ["d1", "d2"], in_use_wait_seconds=6.0)
    assert stack.attempts == {"d1": 3, "d2": 1}
    assert clock.now == 1.0


def _port_client(status: int, detail=None) -> tuple[httpx.AsyncClient, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if status == 204:
            return httpx.Response(204)
        return httpx.Response(status, json={"detail": detail})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://stack")
    return client, seen


PORT_CABLED = {"error": "port_cabled", "connection_count": 1, "connection_ids": ["c9"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "detail"), [(204, None), (404, "Port not found")])
async def test_port_delete_returns_when_the_port_is_gone(status, detail):
    client, seen = _port_client(status, detail)
    async with client:
        await td.delete_port_checked(client, "p1")
    assert seen == ["DELETE /inventory/ports/p1"]


@pytest.mark.asyncio
async def test_port_delete_refused_as_cabled_raises_with_the_body_and_no_retry():
    """The helper clears no cables and does not wait: one call, then the refusal."""
    client, seen = _port_client(409, PORT_CABLED)
    async with client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_port_checked(client, "p1")
    assert seen == ["DELETE /inventory/ports/p1"]
    assert "p1" in str(ei.value) and "409" in str(ei.value)
    assert "port_cabled" in str(ei.value) and "c9" in str(ei.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500, 503])
async def test_port_delete_any_other_refusal_raises_with_the_body(status):
    client, seen = _port_client(status, "Could not verify port is not cabled")
    async with client:
        with pytest.raises(td.DeviceTeardownError) as ei:
            await td.delete_port_checked(client, "p1")
    assert seen == ["DELETE /inventory/ports/p1"]
    assert str(status) in str(ei.value)
    assert "Could not verify port is not cabled" in str(ei.value)
