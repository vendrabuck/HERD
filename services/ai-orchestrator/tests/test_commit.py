"""Unit tests for the AI commit flow.

respx is used to stub httpx calls to cabling and reservations. The tests
drive the endpoint through the real committer to verify happy path,
rollback on canvas save failure, and rollback on reservation failure.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from app import config as config_module
from app.main import app
from httpx import ASGITransport, AsyncClient
from jose import jwt

CABLING_URL = config_module.settings.cabling_service_url.rstrip("/")
RESERVATIONS_URL = config_module.settings.reservations_service_url.rstrip("/")
EXECUTION_URL = config_module.settings.execution_service_url.rstrip("/")

TOPOLOGY_ID = "11111111-1111-1111-1111-111111111111"
RESERVATION_ID = "22222222-2222-2222-2222-222222222222"
DEVICE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DEVICE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _user_token() -> str:
    payload = {
        "sub": "8c67a6b0-000a-4000-8000-000000000001",
        "username": "tester",
        "role": "user",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload,
        config_module.settings.secret_key,
        algorithm=config_module.settings.algorithm,
    )


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _commit_body(**overrides):
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=4)
    body = {
        "topology_name": "AI proposal test",
        "purpose": "HA pair demo",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "devices": [
            {"role": "fw-a", "device_id": DEVICE_A, "position": {"x": 100, "y": 100}},
            {"role": "fw-b", "device_id": DEVICE_B, "position": {"x": 300, "y": 100}},
        ],
        "edges": [{"source_role": "fw-a", "target_role": "fw-b", "layer": "L2"}],
    }
    body.update(overrides)
    return body


async def test_commit_requires_auth(async_client):
    async with async_client as client:
        resp = await client.post("/commit", json=_commit_body())
    assert resp.status_code == 401


async def test_commit_rejects_missing_devices(async_client):
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/commit", json=_commit_body(devices=[]), headers=headers)
    assert resp.status_code == 422


async def test_commit_rejects_end_before_start(async_client):
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    end = start - timedelta(hours=1)
    body = _commit_body(start_time=start.isoformat(), end_time=end.isoformat())
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/commit", json=body, headers=headers)
    assert resp.status_code == 422


async def test_commit_happy_path_creates_topology_and_reservation(async_client):
    captured_canvas: dict = {}

    def _capture_canvas(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured_canvas.update(_json.loads(request.content))
        return httpx.Response(200, json={"id": TOPOLOGY_ID, "canvas_data": {}})

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(
            201, json={"id": TOPOLOGY_ID, "name": "AI proposal test"}
        )
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").mock(side_effect=_capture_canvas)
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={"id": RESERVATION_ID})

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=_commit_body(), headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topology_id"] == TOPOLOGY_ID
    assert body["reservation_id"] == RESERVATION_ID
    assert body["config_results"] == []

    # Canvas includes a node per proposed device and one edge with the chosen layer
    canvas = captured_canvas["canvas_data"]
    assert len(canvas["nodes"]) == 2
    assert {n["data"]["device"]["id"] for n in canvas["nodes"]} == {DEVICE_A, DEVICE_B}
    assert len(canvas["edges"]) == 1
    assert canvas["edges"][0]["data"]["layer"] == "L2"


async def test_commit_rolls_back_topology_when_canvas_save_fails(async_client):
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(500, json={"detail": "db boom"})
        rollback = mock.delete(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(204)

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=_commit_body(), headers=headers)

    assert resp.status_code == 500
    assert "db boom" in resp.json()["detail"]
    assert rollback.called


async def test_commit_rolls_back_topology_when_reservation_fails(async_client):
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        mock.post(f"{RESERVATIONS_URL}/").respond(
            409, json={"detail": "conflicts with another reservation"}
        )
        rollback = mock.delete(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(204)

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=_commit_body(), headers=headers)

    assert resp.status_code == 409
    assert "conflicts" in resp.json()["detail"]
    assert rollback.called


async def test_commit_surfaces_topology_create_failure_without_rollback(async_client):
    """If the first create fails, there is nothing to roll back."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(403, json={"detail": "not authorized"})

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=_commit_body(), headers=headers)

    assert resp.status_code == 403
    assert "not authorized" in resp.json()["detail"]


async def test_commit_apply_configs_calls_execution_per_device(async_client):
    """When apply_configs=true, /execute is called per device with a config."""
    execute_bodies: list[dict] = []

    def _capture_execute(request: httpx.Request) -> httpx.Response:
        import json as _json

        execute_bodies.append(_json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "id": "33333333-3333-3333-3333-333333333333",
                "status": "SUCCESS",
            },
        )

    body = _commit_body(
        devices=[
            {
                "role": "fw-a",
                "device_id": DEVICE_A,
                "config": {"vlan": 10, "ip": "10.0.0.1/24"},
                "connection_type": "Management",
            },
            {"role": "fw-b", "device_id": DEVICE_B},
        ],
        apply_configs=True,
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={"id": RESERVATION_ID})
        mock.post(f"{EXECUTION_URL}/execute").mock(side_effect=_capture_execute)

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=body, headers=headers)

    assert resp.status_code == 200, resp.text
    results = resp.json()["config_results"]
    assert len(results) == 2
    by_role = {r["role"]: r for r in results}
    assert by_role["fw-a"]["status"] == "success"
    assert by_role["fw-a"]["run_id"] == "33333333-3333-3333-3333-333333333333"
    assert by_role["fw-b"]["status"] == "skipped"

    # Only one /execute call, for the device with config
    assert len(execute_bodies) == 1
    called = execute_bodies[0]
    assert called["device_id"] == DEVICE_A
    assert called["action"] == "configure"
    assert called["reservation_id"] == RESERVATION_ID
    assert called["method_kwargs"] == {"vlan": 10, "ip": "10.0.0.1/24"}


async def test_commit_apply_configs_records_failure_without_rollback(async_client):
    """A 403 from /execute is reported as a failed config result, not a rollback."""
    body = _commit_body(
        devices=[
            {
                "role": "fw-a",
                "device_id": DEVICE_A,
                "config": {"vlan": 10},
                "connection_type": "Management",
            }
        ],
        edges=[],
        apply_configs=True,
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={"id": RESERVATION_ID})
        mock.post(f"{EXECUTION_URL}/execute").respond(403, json={"detail": "admin required"})

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=body, headers=headers)

    assert resp.status_code == 200, resp.text
    body_out = resp.json()
    assert body_out["topology_id"] == TOPOLOGY_ID
    assert body_out["reservation_id"] == RESERVATION_ID
    results = body_out["config_results"]
    assert len(results) == 1
    assert results[0]["status"] == "failed"
    assert "admin required" in (results[0]["error"] or "")


async def test_commit_skips_execution_when_apply_configs_false(async_client):
    """apply_configs=false means /execute is not called, even if configs exist."""
    body = _commit_body(
        devices=[
            {
                "role": "fw-a",
                "device_id": DEVICE_A,
                "config": {"vlan": 10},
                "connection_type": "Management",
            }
        ],
        edges=[],
        apply_configs=False,
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={"id": RESERVATION_ID})
        # No /execute route is registered; if it's called, respx raises.

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=body, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["config_results"] == []


async def test_commit_forwards_user_jwt_to_upstream(async_client):
    captured_headers: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_headers["topology"] = request.headers.get("authorization", "")
        return httpx.Response(201, json={"id": TOPOLOGY_ID})

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").mock(side_effect=_capture)
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={"id": RESERVATION_ID})

        token = _user_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with async_client as client:
            resp = await client.post("/commit", json=_commit_body(), headers=headers)

    assert resp.status_code == 200
    assert captured_headers["topology"] == f"Bearer {token}"


# --- Config validation (B8): LLM-proposed kwargs must match the registry ---


async def test_commit_rejects_config_without_connection_type(async_client):
    """Config present but connection_type missing -> 422 before any upstream write."""
    body = _commit_body(
        devices=[
            {
                "role": "fw-a",
                "device_id": DEVICE_A,
                "config": {"vlan": 10},
            }
        ],
        edges=[],
    )
    # No respx mock: a 422 must be returned before any upstream call fires.
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/commit", json=body, headers=headers)
    assert resp.status_code == 422
    assert "connection_type" in resp.json()["detail"]


async def test_commit_rejects_config_on_unsupported_connection_type(async_client):
    """Config on a connection_type without a configure schema -> 422."""
    body = _commit_body(
        devices=[
            {
                "role": "sw-a",
                "device_id": DEVICE_A,
                "config": {"vlan": 10},
                "connection_type": "Layer 1 Switch",
            }
        ],
        edges=[],
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/commit", json=body, headers=headers)
    assert resp.status_code == 422
    assert "Layer 1 Switch" in resp.json()["detail"]


async def test_commit_rejects_unknown_config_key(async_client):
    """Config with a key not in the Management schema -> 422."""
    body = _commit_body(
        devices=[
            {
                "role": "fw-a",
                "device_id": DEVICE_A,
                "config": {"admin_password": "sneaky"},
                "connection_type": "Management",
            }
        ],
        edges=[],
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/commit", json=body, headers=headers)
    assert resp.status_code == 422
    # jsonschema's additionalProperties error mentions the offending key.
    assert "admin_password" in resp.json()["detail"]


async def test_commit_rejects_out_of_range_vlan(async_client):
    """Vlan outside 1..4094 -> 422."""
    body = _commit_body(
        devices=[
            {
                "role": "fw-a",
                "device_id": DEVICE_A,
                "config": {"vlan": 9999},
                "connection_type": "Management",
            }
        ],
        edges=[],
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post("/commit", json=body, headers=headers)
    assert resp.status_code == 422


async def test_commit_allows_no_config_on_unknown_connection_type(async_client):
    """No config present -> connection_type is ignored, commit proceeds."""
    body = _commit_body(
        devices=[
            {
                "role": "sw-a",
                "device_id": DEVICE_A,
                "connection_type": "Layer 2 Switch",
            },
        ],
        edges=[],
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={"id": RESERVATION_ID})

        headers = {"Authorization": f"Bearer {_user_token()}"}
        async with async_client as client:
            resp = await client.post("/commit", json=body, headers=headers)

    assert resp.status_code == 200
