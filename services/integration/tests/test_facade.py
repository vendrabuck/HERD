"""Facade unit tests.

The reservations upstream is mocked: app.routers.reservations.AsyncClient is
monkeypatched with a fake client driven by a per-test handler, so no real network is
touched. The handler also captures the forwarded request so we can assert that the
caller's Authorization header and the translated body reach the upstream. The facade
imports AsyncClient by name, so patching it does not clobber the real httpx.AsyncClient
the test itself uses to drive the ASGI app.
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from app.main import app
from app.routers import reservations as facade
from httpx import ASGITransport, AsyncClient
from jose import jwt

SECRET = "test-secret"

# Captured by the fake client for forwarding assertions.
CAPTURED: dict = {}


def _token(role: str = "user") -> str:
    return jwt.encode({"sub": str(uuid.uuid4()), "role": role}, SECRET, algorithm="HS256")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sample_internal(reservation_id: str | None = None) -> dict:
    """A reservations-service response payload (richer than the v1 view)."""
    now = datetime.now(timezone.utc)
    return {
        "id": reservation_id or str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "owner_name": "alice",
        "device_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        "topology_id": None,
        "topology_type": "adhoc",
        "purpose": "test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "status": "PENDING",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "modified_by": None,
        "purpose_category": None,
    }


def _install_handler(monkeypatch, handler):
    """Replace the router's AsyncClient with a fake driven by `handler`.

    `handler(method, url, headers, json, params)` returns an httpx.Response or raises
    an httpx.RequestError (to simulate an unreachable upstream).
    """

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, headers=None, json=None, params=None):
            CAPTURED.clear()
            CAPTURED.update(method=method, url=url, headers=headers, json=json, params=params)
            return handler(method, url, headers, json, params)

    monkeypatch.setattr(facade, "AsyncClient", _FakeAsyncClient)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_create_forwards_jwt_and_translated_body(monkeypatch):
    internal = _sample_internal()

    def handler(method, url, headers, json, params):
        assert method == "POST"
        assert url.endswith("/")
        return httpx.Response(201, json=internal)

    _install_handler(monkeypatch, handler)
    token = _token()
    dev_ids = [str(uuid.uuid4())]
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": dev_ids,
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=2)).isoformat(),
        "purpose": "lab work",
        "topology_id": None,
    }
    async with _client() as c:
        resp = await c.post("/reservations", json=body, headers=_auth(token))

    assert resp.status_code == 201
    # JWT forwarded verbatim to the upstream.
    assert CAPTURED["headers"]["Authorization"] == f"Bearer {token}"
    # Translated body reached the upstream.
    assert CAPTURED["json"]["device_ids"] == dev_ids
    assert CAPTURED["json"]["purpose"] == "lab work"
    # v1 response is the frozen subset, not the internal payload.
    data = resp.json()
    assert set(data.keys()) == {
        "id",
        "status",
        "device_ids",
        "topology_id",
        "start_time",
        "end_time",
        "created_at",
        "purpose_category",
    }
    assert data["id"] == internal["id"]
    assert data["status"] == "PENDING"


async def test_create_forwards_and_returns_purpose_category(monkeypatch):
    """purpose_category (issue #646 phase 1) round-trips through the facade
    both directions: forwarded on create, and echoed back in the v1 view."""
    internal = _sample_internal()
    internal["purpose_category"] = "qa_regression"

    def handler(method, url, headers, json, params):
        assert method == "POST"
        return httpx.Response(201, json=internal)

    _install_handler(monkeypatch, handler)
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [str(uuid.uuid4())],
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=2)).isoformat(),
        "purpose": "lab work",
        "purpose_category": "qa_regression",
    }
    async with _client() as c:
        resp = await c.post("/reservations", json=body, headers=_auth(_token()))

    assert resp.status_code == 201
    # Forwarded to the upstream create body.
    assert CAPTURED["json"]["purpose_category"] == "qa_regression"
    # Echoed back in the v1 response.
    assert resp.json()["purpose_category"] == "qa_regression"


async def test_create_purpose_category_defaults_to_none(monkeypatch):
    """Omitting purpose_category forwards None rather than a missing key, and
    the v1 response carries None back when the upstream reports unclassified."""
    internal = _sample_internal()

    def handler(method, url, headers, json, params):
        return httpx.Response(201, json=internal)

    _install_handler(monkeypatch, handler)
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [str(uuid.uuid4())],
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=2)).isoformat(),
    }
    async with _client() as c:
        resp = await c.post("/reservations", json=body, headers=_auth(_token()))

    assert resp.status_code == 201
    assert CAPTURED["json"]["purpose_category"] is None
    assert resp.json()["purpose_category"] is None


async def test_list_maps_to_v1_paginated(monkeypatch):
    items = [_sample_internal(), _sample_internal()]

    def handler(method, url, headers, json, params):
        assert method == "GET"
        return httpx.Response(200, json={"items": items, "total": 2, "skip": 0, "limit": 50})

    _install_handler(monkeypatch, handler)
    async with _client() as c:
        resp = await c.get("/reservations", headers=_auth(_token()))

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert set(data["items"][0].keys()) == {
        "id",
        "status",
        "device_ids",
        "topology_id",
        "start_time",
        "end_time",
        "created_at",
        "purpose_category",
    }


async def test_get_status_maps(monkeypatch):
    internal = _sample_internal()

    def handler(method, url, headers, json, params):
        assert method == "GET"
        assert url.endswith(f"/{internal['id']}")
        return httpx.Response(200, json=internal)

    _install_handler(monkeypatch, handler)
    async with _client() as c:
        resp = await c.get(f"/reservations/{internal['id']}", headers=_auth(_token()))

    assert resp.status_code == 200
    assert resp.json()["id"] == internal["id"]


async def test_cancel_returns_204(monkeypatch):
    rid = str(uuid.uuid4())

    def handler(method, url, headers, json, params):
        assert method == "DELETE"
        return httpx.Response(204)

    _install_handler(monkeypatch, handler)
    async with _client() as c:
        resp = await c.delete(f"/reservations/{rid}", headers=_auth(_token()))

    assert resp.status_code == 204
    assert resp.content == b""


async def test_release_maps(monkeypatch):
    internal = _sample_internal()
    internal["status"] = "COMPLETED"

    def handler(method, url, headers, json, params):
        assert method == "PUT"
        assert url.endswith(f"/{internal['id']}/release")
        return httpx.Response(200, json=internal)

    _install_handler(monkeypatch, handler)
    async with _client() as c:
        resp = await c.put(f"/reservations/{internal['id']}/release", headers=_auth(_token()))

    assert resp.status_code == 200
    assert resp.json()["status"] == "COMPLETED"


async def test_wiring_status_relays_layered_payload_verbatim(monkeypatch):
    """GET /reservations/{id}/wiring-status forwards the JWT and relays the layered
    payload verbatim (ADR 0009 phase 8): rows keep their `layer` tags and
    layer-specific fields, and the reservation-level markers pass through untouched."""
    rid = str(uuid.uuid4())
    upstream = {
        "reservation_id": rid,
        "last_applied_fork_version": 3,
        "frozen": False,
        "connections": [
            {
                "id": str(uuid.uuid4()),
                "switch_device_id": str(uuid.uuid4()),
                "layer": "l1",
                "port_a": "0/0/1",
                "port_b": "0/0/2",
                "status": "ACTIVE",
                "intended": "ACTIVE",
                "attempts": 1,
                "last_error": None,
                "retryable": False,
            },
            {
                "id": str(uuid.uuid4()),
                "switch_device_id": str(uuid.uuid4()),
                "layer": "l2",
                "port": "p3",
                "vlan": 101,
                "status": "FAILED",
                "intended": "ACTIVE",
                "attempts": 2,
                "last_error": "add_to_vlan boom",
                "retryable": True,
            },
            {
                "id": str(uuid.uuid4()),
                "switch_device_id": str(uuid.uuid4()),
                "layer": "l3",
                "route_count": 2,
                "status": "ACTIVE",
                "intended": "ACTIVE",
                "attempts": 1,
                "last_error": None,
                "retryable": False,
            },
        ],
    }

    def handler(method, url, headers, json, params):
        assert method == "GET"
        assert url.endswith(f"/{rid}/wiring-status")
        return httpx.Response(200, json=upstream)

    _install_handler(monkeypatch, handler)
    token = _token()
    async with _client() as c:
        resp = await c.get(f"/reservations/{rid}/wiring-status", headers=_auth(token))

    assert resp.status_code == 200
    assert CAPTURED["headers"]["Authorization"] == f"Bearer {token}"
    assert resp.json() == upstream


async def test_wiring_status_requires_jwt(monkeypatch):
    async with _client() as c:
        resp = await c.get(f"/reservations/{uuid.uuid4()}/wiring-status")
    assert resp.status_code in (401, 403)


async def test_missing_jwt_is_rejected(monkeypatch):
    # Auth rejects before any upstream call. FastAPI's HTTPBearer (auto_error=True,
    # the router's default) emits exactly this status, body, and header; pin it so a
    # regression to a different auth dependency (or auto_error=False) is caught.
    async with _client() as c:
        resp = await c.get("/reservations")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_validation_error_envelope_shape(monkeypatch):
    """A 422 from the facade's own request validation (never reaching the upstream)
    matches FastAPI's default envelope: this is an external contract per
    docs/EXTERNAL_API.md, so pin its shape structurally rather than by example."""
    async with _client() as c:
        resp = await c.post("/reservations", json={}, headers=_auth(_token()))

    assert resp.status_code == 422
    body = resp.json()
    assert set(body.keys()) == {"detail"}
    assert isinstance(body["detail"], list)
    assert len(body["detail"]) > 0
    for item in body["detail"]:
        assert set(item.keys()) == {"type", "loc", "msg", "input"}


async def test_invalid_jwt_is_401(monkeypatch):
    async with _client() as c:
        resp = await c.get("/reservations", headers=_auth("not-a-real-token"))
    assert resp.status_code == 401


@pytest.mark.parametrize("upstream_status", [403, 404, 409, 422])
async def test_upstream_error_is_propagated(monkeypatch, upstream_status):
    def handler(method, url, headers, json, params):
        return httpx.Response(upstream_status, json={"detail": "upstream says no"})

    _install_handler(monkeypatch, handler)
    async with _client() as c:
        resp = await c.get(f"/reservations/{uuid.uuid4()}", headers=_auth(_token()))

    assert resp.status_code == upstream_status
    assert resp.json()["detail"] == "upstream says no"


async def test_upstream_unreachable_is_503(monkeypatch):
    def handler(method, url, headers, json, params):
        raise httpx.ConnectError("connection refused")

    _install_handler(monkeypatch, handler)
    async with _client() as c:
        resp = await c.get(f"/reservations/{uuid.uuid4()}", headers=_auth(_token()))

    assert resp.status_code == 503
