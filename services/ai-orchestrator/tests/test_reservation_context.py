"""Tests for the iter-2 reservation seed gatherer and renderer."""

import asyncio
import uuid

import httpx
import pytest
from app.services import reservation_context as ctx_module
from app.services.reservation_context import (
    ReservationNotFoundError,
    ReservationSeed,
    gather_reservation_seed,
    render_seed_block,
)

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TOPOLOGY_ID = "22222222-2222-2222-2222-222222222222"
DEVICE_A = "33333333-3333-3333-3333-333333333333"
DEVICE_B = "44444444-4444-4444-4444-444444444444"


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class PatchedAsync(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ctx_module.httpx, "AsyncClient", PatchedAsync)


def _reservation_payload(*, device_ids=None, topology_id=TOPOLOGY_ID, extra=None):
    data = {
        "id": str(RESERVATION_ID),
        "status": "ACTIVE",
        "start_time": "2026-05-19T09:00:00Z",
        "end_time": "2026-05-19T17:00:00Z",
        "topology_id": topology_id,
        "topology_type": "PHYSICAL",
        "purpose": "Smoke test",
        "owner_name": "Jordan Lee",
        "device_ids": device_ids or [DEVICE_A, DEVICE_B],
        "user_id": "00000000-0000-0000-0000-000000000000",
        "created_at": "2026-05-18T00:00:00Z",
        "updated_at": "2026-05-19T00:00:00Z",
    }
    if extra:
        data.update(extra)
    return data


def _device_payload(
    device_id,
    name,
    template="firewall",
    vendor="Juniper Networks",
    model="EX3400",
):
    return {
        "id": device_id,
        "name": name,
        "template_id": "tpl-fw",
        "template_name": template,
        "template_vendor": vendor,
        "template_model": model,
        "template_part_number": None,
        "topology_type": "PHYSICAL",
        "status": "RESERVED",
        "field_data": {"management_ip": "10.0.0.1", "password": "DO-NOT-LEAK"},
        "exclusive": True,
        "connection_type": "Management",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _make_handler(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for predicate, response in routes:
            if predicate(request):
                return response
        return httpx.Response(404, json={"detail": "unmocked route"})

    return handler


async def test_seed_gather_returns_thin_bundle(monkeypatch):
    """Seed gather pulls reservation + devices but no topology fetch."""
    topology_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/{RESERVATION_ID}") and "topologies" not in path:
            return httpx.Response(200, json=_reservation_payload())
        if path.endswith(f"/devices/{DEVICE_A}"):
            return httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a"))
        if path.endswith(f"/devices/{DEVICE_B}"):
            return httpx.Response(200, json=_device_payload(DEVICE_B, "fw-b", template="switch"))
        if "topologies" in path:
            topology_calls.append(path)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "unmocked"})

    _patch_httpx(monkeypatch, handler)

    seed = await gather_reservation_seed("test-token", RESERVATION_ID)

    assert isinstance(seed, ReservationSeed)
    assert seed.reservation["status"] == "ACTIVE"
    assert seed.reservation["owner_name"] == "Jordan Lee"
    assert "device_ids" not in seed.reservation  # narrower whitelist than iter 1
    assert "user_id" not in seed.reservation

    assert len(seed.devices) == 2
    assert {d["name"] for d in seed.devices} == {"fw-a", "fw-b"}
    for dev in seed.devices:
        # Only the seed device fields, no field_data, no connection_type, no exclusive
        assert set(dev.keys()) <= {
            "id",
            "name",
            "template_name",
            "template_vendor",
            "template_model",
            "status",
        }
        assert "field_data" not in dev
        assert "password" not in str(dev)
        assert dev["template_vendor"] == "Juniper Networks"
        assert dev["template_model"] == "EX3400"

    # No topology endpoint call was made even though topology_id was set
    assert topology_calls == []


async def test_seed_gather_raises_when_reservation_not_found(monkeypatch):
    routes = [
        (
            lambda r: r.url.path.endswith(f"/{RESERVATION_ID}"),
            httpx.Response(404, json={"detail": "Reservation not found"}),
        ),
    ]
    _patch_httpx(monkeypatch, _make_handler(routes))

    with pytest.raises(ReservationNotFoundError):
        await gather_reservation_seed("test-token", RESERVATION_ID)


async def test_seed_gather_skips_missing_devices(monkeypatch):
    routes = [
        (
            lambda r: r.url.path.endswith(f"/{RESERVATION_ID}"),
            httpx.Response(200, json=_reservation_payload(topology_id=None)),
        ),
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}"),
            httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a")),
        ),
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_B}"),
            httpx.Response(404),
        ),
    ]
    _patch_httpx(monkeypatch, _make_handler(routes))

    seed = await gather_reservation_seed("test-token", RESERVATION_ID)
    assert len(seed.devices) == 1
    assert seed.devices[0]["name"] == "fw-a"


async def test_seed_gather_respects_deadline(monkeypatch):
    monkeypatch.setattr(ctx_module, "GATHER_DEADLINE_SECONDS", 0.1)

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return httpx.Response(200, json={})

    _patch_httpx(monkeypatch, slow_handler)

    from app.services.reservation_context import ContextDeadlineExceededError

    with pytest.raises(ContextDeadlineExceededError):
        await gather_reservation_seed("test-token", RESERVATION_ID)


def test_render_seed_emits_xml_blocks_without_topology():
    seed = ReservationSeed(
        reservation={
            "id": str(RESERVATION_ID),
            "status": "ACTIVE",
            "purpose": "Smoke test",
            "owner_name": "Jordan Lee",
        },
        devices=[
            {"id": DEVICE_A, "name": "fw-a", "template_name": "firewall", "status": "RESERVED"},
            {"id": DEVICE_B, "name": "fw-b", "template_name": "switch", "status": "RESERVED"},
        ],
    )
    rendered = render_seed_block(seed)
    assert "<reservation>" in rendered and "</reservation>" in rendered
    assert "<devices>" in rendered and "</devices>" in rendered
    assert "<topology>" not in rendered  # seed never includes topology
    assert "status: ACTIVE" in rendered
    assert "name=fw-a" in rendered
    assert "template_name=switch" in rendered


def test_render_seed_handles_no_devices():
    seed = ReservationSeed(reservation={"id": str(RESERVATION_ID), "status": "ACTIVE"})
    rendered = render_seed_block(seed)
    assert "(no devices)" in rendered
    assert "<topology>" not in rendered
