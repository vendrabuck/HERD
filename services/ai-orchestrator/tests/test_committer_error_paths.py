"""Error-path coverage for the AI commit flow (app/services/committer.py).

test_commit.py covers the happy path and the CommitError rollbacks through the
ASGI route. This module pins the remaining error branches by calling the
committer helpers directly with hand-built httpx stubs: the _detail body
parser, the canvas edge-skip on a dangling role, the rollback-delete that
swallows its own failure, the per-device apply branches (request exception and
malformed JSON), and the unexpected-non-CommitError rollback in
commit_proposal.
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from app import config as config_module
from app.schemas.generate import CommitDevice, CommitEdge, CommitElement, CommitRequest
from app.services import committer
from app.services.committer import (
    CommitError,
    _apply_configs,
    _build_canvas_data,
    _delete_topology,
    _detail,
    _fetch_device_ports,
)

CABLING_URL = config_module.settings.cabling_service_url.rstrip("/")
RESERVATIONS_URL = config_module.settings.reservations_service_url.rstrip("/")
EXECUTION_URL = config_module.settings.execution_service_url.rstrip("/")

TOPOLOGY_ID = "11111111-1111-1111-1111-111111111111"
RESERVATION_ID = "22222222-2222-2222-2222-222222222222"
DEVICE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _req(**overrides) -> CommitRequest:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=4)
    base = dict(
        topology_name="t",
        purpose="p",
        start_time=start,
        end_time=end,
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        edges=[],
    )
    base.update(overrides)
    return CommitRequest(**base)


# --- _detail: response-body parsing (lines 45-51) ---


def test_detail_non_json_body_falls_back_to_text():
    resp = httpx.Response(500, text="raw server error")
    assert _detail(resp) == "raw server error"


def test_detail_empty_non_json_body_falls_back_to_status():
    resp = httpx.Response(503, content=b"")
    # No text and not JSON: the f-string fallback wins.
    assert _detail(resp) == "HTTP 503"


def test_detail_dict_body_uses_detail_key():
    resp = httpx.Response(409, json={"detail": "conflict here"})
    assert _detail(resp) == "conflict here"


def test_detail_non_dict_json_body_stringifies():
    resp = httpx.Response(400, json=["a", "b"])
    assert _detail(resp) == "['a', 'b']"


# --- _build_canvas_data: dangling edge role is skipped (line 88) ---


async def test_build_canvas_skips_edge_with_unknown_role():
    req = _req(
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        edges=[CommitEdge(source_role="fw-a", target_role="does-not-exist", layer="L2")],
    )
    async with httpx.AsyncClient() as client:
        canvas = await _build_canvas_data(client, {"Authorization": "Bearer t"}, req)
    # One node, but the edge references a role with no node, so it is dropped.
    # No element in this proposal, so no ports HTTP call ever fires (proven by
    # the bare client above having no respx mock registered).
    assert len(canvas["nodes"]) == 1
    assert canvas["edges"] == []


# --- _build_canvas_data: network elements (issue #632) -----------------


def _ports_route(mock, device_id: str, ports: list[dict]):
    """Register a respx route for GET /devices/{device_id}/ports on `mock`."""
    url = f"{config_module.settings.inventory_service_url.rstrip('/')}/devices/{device_id}/ports"
    return mock.get(url).respond(200, json=ports)


async def test_build_canvas_device_to_device_path_is_byte_for_byte_unchanged():
    """Pins the pre-#632 device-to-device edge shape exactly."""
    req = _req(
        devices=[
            CommitDevice(role="fw-a", device_id=DEVICE_A, position={"x": 1.0, "y": 2.0}),
            CommitDevice(role="fw-b", device_id="cccccccc-cccc-cccc-cccc-cccccccccccc"),
        ],
        edges=[CommitEdge(source_role="fw-a", target_role="fw-b", layer="L3")],
    )
    async with httpx.AsyncClient() as client:
        canvas = await _build_canvas_data(client, {"Authorization": "Bearer t"}, req)
    assert canvas["selectedEdgeLayer"] == "L2"
    assert len(canvas["nodes"]) == 2
    node_a = next(n for n in canvas["nodes"] if n["data"]["device"]["id"] == DEVICE_A)
    assert node_a["type"] == "deviceNode"
    assert node_a["position"] == {"x": 1.0, "y": 2.0}
    assert node_a["data"] == {
        "device": {"id": DEVICE_A},
        "label": "fw-a",
        "topologyType": "PHYSICAL",
    }
    assert len(canvas["edges"]) == 1
    edge = canvas["edges"][0]
    assert set(edge.keys()) == {"id", "source", "target", "data"}
    assert edge["data"] == {"layer": "L3"}


async def test_build_canvas_element_node_shape():
    req = _req(
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        elements=[
            CommitElement(
                role="mgmt-seg",
                element_type="vlan_segment",
                label="Mgmt VLAN",
                attrs={"vlan_id": 100},
            )
        ],
        edges=[],
    )
    async with httpx.AsyncClient() as client:
        canvas = await _build_canvas_data(client, {"Authorization": "Bearer t"}, req)
    element_nodes = [n for n in canvas["nodes"] if n["type"] == "networkElementNode"]
    assert len(element_nodes) == 1
    node = element_nodes[0]
    uuid.UUID(node["id"])  # a real uuid4 canvas node id
    element = node["data"]["element"]
    assert set(element.keys()) == {"id", "element_type", "label", "attrs"}
    uuid.UUID(element["id"])  # a real uuid4 element id, distinct from node id
    assert element["id"] != node["id"]
    assert element["element_type"] == "vlan_segment"
    assert element["label"] == "Mgmt VLAN"
    assert element["attrs"] == {"vlan_id": 100}


async def test_build_canvas_attachment_edge_has_device_as_source_with_chosen_port():
    req = _req(
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        elements=[
            CommitElement(role="mgmt-seg", element_type="vlan_segment", label="Mgmt", attrs={})
        ],
        edges=[CommitEdge(source_role="fw-a", target_role="mgmt-seg", layer="L2")],
    )
    ports = [
        {"id": "port-10", "name": "eth10"},
        {"id": "port-2", "name": "eth2"},
    ]
    with respx.mock(assert_all_called=True) as mock:
        _ports_route(mock, DEVICE_A, ports)
        async with httpx.AsyncClient() as client:
            canvas = await _build_canvas_data(client, {"Authorization": "Bearer t"}, req)

    assert len(canvas["edges"]) == 1
    edge = canvas["edges"][0]
    device_node = next(n for n in canvas["nodes"] if n["type"] == "deviceNode")
    element_node = next(n for n in canvas["nodes"] if n["type"] == "networkElementNode")
    assert edge["source"] == device_node["id"]
    assert edge["target"] == element_node["id"]
    # Natural port order picks eth2 before eth10, not lexicographic order
    # (which would put "eth10" before "eth2").
    assert edge["data"] == {
        "layer": "L2",
        "source_port_id": "port-2",
        "source_port_name": "eth2",
    }


async def test_build_canvas_two_attachments_from_one_device_get_distinct_ports():
    req = _req(
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        elements=[
            CommitElement(role="mgmt-seg", element_type="vlan_segment", label="Mgmt", attrs={}),
            CommitElement(role="data-seg", element_type="subnet", label="Data", attrs={}),
        ],
        edges=[
            CommitEdge(source_role="fw-a", target_role="mgmt-seg", layer="L2"),
            CommitEdge(source_role="fw-a", target_role="data-seg", layer="L2"),
        ],
    )
    ports = [{"id": "port-1", "name": "eth1"}, {"id": "port-2", "name": "eth2"}]
    with respx.mock(assert_all_called=True) as mock:
        route = _ports_route(mock, DEVICE_A, ports)
        async with httpx.AsyncClient() as client:
            canvas = await _build_canvas_data(client, {"Authorization": "Bearer t"}, req)
    # Ports are cached per device: one attaching device with two attachments
    # triggers exactly one fetch, not two.
    assert route.call_count == 1

    assert len(canvas["edges"]) == 2
    used_ports = {edge["data"]["source_port_name"] for edge in canvas["edges"]}
    assert used_ports == {"eth1", "eth2"}


async def test_build_canvas_device_with_no_ports_skips_attachment_with_warning(caplog):
    import logging

    req = _req(
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        elements=[
            CommitElement(role="mgmt-seg", element_type="vlan_segment", label="Mgmt", attrs={})
        ],
        edges=[CommitEdge(source_role="fw-a", target_role="mgmt-seg", layer="L2")],
    )
    with respx.mock(assert_all_called=True) as mock:
        _ports_route(mock, DEVICE_A, [])
        with caplog.at_level(logging.WARNING):
            async with httpx.AsyncClient() as client:
                canvas = await _build_canvas_data(client, {"Authorization": "Bearer t"}, req)

    # The element node still exists (it just has no attachment), but the
    # edge is dropped rather than emitted with an empty source_port_name.
    assert any(n["type"] == "networkElementNode" for n in canvas["nodes"])
    assert canvas["edges"] == []
    assert any(r.message == "ai_commit_element_attachment_skipped_no_port" for r in caplog.records)


# --- _fetch_device_ports: fail closed on an unanswerable question (#717) ---


async def test_fetch_device_ports_404_is_treated_as_no_ports():
    """A genuine 404 means the device has no ports: not an error."""
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"{config_module.settings.inventory_service_url.rstrip('/')}/devices/{DEVICE_A}/ports"
        ).respond(404, json={"detail": "not found"})
        async with httpx.AsyncClient() as client:
            ports = await _fetch_device_ports(client, {"Authorization": "Bearer t"}, DEVICE_A)
    assert ports == []


async def test_fetch_device_ports_5xx_raises_commit_error(caplog):
    """A 5xx from inventory means it could not answer the question at all,
    which is not the same as a portless device: it must fail the whole
    commit closed rather than silently drop the attachment."""
    import logging

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"{config_module.settings.inventory_service_url.rstrip('/')}/devices/{DEVICE_A}/ports"
        ).respond(503, json={"detail": "inventory unavailable"})
        with caplog.at_level(logging.WARNING):
            async with httpx.AsyncClient() as client:
                with pytest.raises(CommitError) as exc_info:
                    await _fetch_device_ports(client, {"Authorization": "Bearer t"}, DEVICE_A)
    assert exc_info.value.status_code == 503
    assert any(r.message == "ai_commit_device_ports_fetch_failed" for r in caplog.records)


async def test_fetch_device_ports_transport_failure_raises_commit_error(caplog):
    """An unreachable inventory is the same failure class as a 5xx: fail
    closed, do not treat it as a portless device."""
    import logging

    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.get(
                f"{config_module.settings.inventory_service_url.rstrip('/')}"
                f"/devices/{DEVICE_A}/ports"
            ).mock(side_effect=httpx.ConnectError("inventory unreachable"))
            with caplog.at_level(logging.WARNING):
                with pytest.raises(CommitError) as exc_info:
                    await _fetch_device_ports(client, {"Authorization": "Bearer t"}, DEVICE_A)
    assert exc_info.value.status_code == 503
    assert any(r.message == "ai_commit_device_ports_fetch_failed" for r in caplog.records)


# --- _delete_topology: rollback delete swallows its own failure (127-128) ---


async def test_delete_topology_swallows_exception(caplog):
    import logging

    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.delete(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").mock(
                side_effect=httpx.ConnectError("cabling down")
            )
            with caplog.at_level(logging.ERROR):
                # Must NOT raise: a failed rollback only logs.
                await _delete_topology(client, {"Authorization": "Bearer t"}, TOPOLOGY_ID)
    assert any(r.message == "rollback_topology_delete_failed" for r in caplog.records)


# --- _apply_configs: per-device failure branches (182-191, 204-205) ---


async def test_apply_configs_records_request_exception_as_failed():
    req = _req(
        devices=[
            CommitDevice(
                role="fw-a", device_id=DEVICE_A, config={"vlan": 10}, connection_type="Management"
            )
        ],
        apply_configs=True,
    )
    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.post(f"{EXECUTION_URL}/execute").mock(
                side_effect=httpx.ConnectError("execution unreachable")
            )
            results = await _apply_configs(
                client, {"Authorization": "Bearer t"}, req, "user-1", RESERVATION_ID
            )
    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].error.startswith("request failed: ")
    assert "execution unreachable" in results[0].error


async def test_apply_configs_handles_non_json_success_body():
    """A 200 with a non-JSON body defaults run_status to SUCCESS (payload={})."""
    req = _req(
        devices=[
            CommitDevice(
                role="fw-a", device_id=DEVICE_A, config={"vlan": 10}, connection_type="Management"
            )
        ],
        apply_configs=True,
    )
    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.post(f"{EXECUTION_URL}/execute").respond(200, text="not json")
            results = await _apply_configs(
                client, {"Authorization": "Bearer t"}, req, "user-1", RESERVATION_ID
            )
    assert len(results) == 1
    # payload={} so status defaults to SUCCESS, run_id None, error None.
    assert results[0].status == "success"
    assert results[0].run_id is None
    assert results[0].error is None


async def test_apply_configs_non_success_status_marks_failed():
    """A 200 whose JSON status is not SUCCESS is recorded as failed."""
    req = _req(
        devices=[
            CommitDevice(
                role="fw-a", device_id=DEVICE_A, config={"vlan": 10}, connection_type="Management"
            )
        ],
        apply_configs=True,
    )
    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.post(f"{EXECUTION_URL}/execute").respond(
                200, json={"id": "run-1", "status": "FAILURE", "error": "driver refused"}
            )
            results = await _apply_configs(
                client, {"Authorization": "Bearer t"}, req, "user-1", RESERVATION_ID
            )
    assert results[0].status == "failed"
    assert results[0].error == "driver refused"
    assert results[0].run_id == "run-1"


# --- commit_proposal: unexpected non-CommitError triggers rollback (244-246) ---


async def test_commit_unexpected_error_rolls_back_and_wraps_502(monkeypatch):
    """If a step raises something other than CommitError (e.g. a KeyError on a
    malformed upstream body), commit_proposal deletes the topology and re-raises
    as a 502 CommitError, never leaking the raw exception as a 500.
    """
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        # Reservation responds 201 but with NO "id" key: _create_reservation does
        # resp.json()["id"], raising KeyError, which is the non-CommitError branch.
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={})
        rollback = mock.delete(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(204)

        with pytest.raises(CommitError) as exc:
            await committer.commit_proposal(_req(), "user-bearer", "user-1")

    assert exc.value.status_code == 502
    assert "Unexpected upstream failure" in exc.value.message
    assert rollback.called
