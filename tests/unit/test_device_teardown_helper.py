"""Unit tests for the strict device teardown helper (issue #940).

`tests/integration/_device_teardown.py` backs the integration fixtures that delete
devices. The stack is faked with `httpx.MockTransport`, so these run with no
stack: they pin the cleanup of leftover cables, the bounded wait on a live
reservation, and that every other refusal fails loudly with the response body.
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
