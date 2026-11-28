"""The AI commit's canvas has the shape cabling holds every canvas to (TOPO-SHAPE-4).

`committer._build_canvas_data` builds the canvas the commit PUTs to cabling, so a
canvas it builds that cabling's check refuses would fail every commit with a 422.
This calls the real builder for each kind of node and edge it writes and judges its
output with cabling's own check, loaded by path from the repository (cabling's `app`
package is not importable beside this service's).
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import respx
from app import config as config_module
from app.schemas.generate import CommitDevice, CommitEdge, CommitElement, CommitRequest
from app.services.committer import _build_canvas_data

_CHECK_PATH = (
    Path(__file__).resolve().parents[2] / "cabling" / "app" / "services" / "canvas_shape.py"
)
DEVICE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DEVICE_B = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _canvas_problem():
    name = "cabling_canvas_shape_for_commit_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _CHECK_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].canvas_problem


def _req(**fields) -> CommitRequest:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    return CommitRequest(
        topology_name="t",
        purpose="p",
        start_time=start,
        end_time=start + timedelta(hours=4),
        **fields,
    )


async def test_every_kind_of_commit_node_and_edge_has_the_shape():
    req = _req(
        devices=[
            CommitDevice(role="fw-a", device_id=DEVICE_A, position={"x": 1.0, "y": 2.0}),
            CommitDevice(role="fw-b", device_id=DEVICE_B),
        ],
        elements=[
            CommitElement(role="mgmt", element_type="vlan_segment", label="Mgmt", attrs={"v": 1}),
            CommitElement(role="data", element_type="subnet", label="Data", attrs={}),
        ],
        edges=[
            CommitEdge(source_role="fw-a", target_role="fw-b", layer="L3"),
            CommitEdge(source_role="fw-a", target_role="mgmt", layer="L2"),
            CommitEdge(source_role="data", target_role="fw-b", layer="L2"),
        ],
    )
    inventory = config_module.settings.inventory_service_url.rstrip("/")
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{inventory}/devices/{DEVICE_A}/ports").respond(
            200, json=[{"id": "p-1", "name": "eth1"}]
        )
        mock.get(f"{inventory}/devices/{DEVICE_B}/ports").respond(
            200, json=[{"id": "p-2", "name": "eth2"}]
        )
        async with httpx.AsyncClient() as client:
            canvas = await _build_canvas_data(client, {"Authorization": "Bearer t"}, req)
    # Two device nodes, two element nodes, a device-to-device edge, and two
    # attachment edges, one built from each direction.
    assert sorted(node["type"] for node in canvas["nodes"]) == [
        "deviceNode",
        "deviceNode",
        "networkElementNode",
        "networkElementNode",
    ]
    assert len(canvas["edges"]) == 3
    assert _canvas_problem()(canvas) is None


def test_the_check_loaded_here_refuses_what_it_refuses_in_cabling():
    """The by-path load is the real check, not a stand-in that passes everything."""
    problem = _canvas_problem()({"nodes": [{"data": {"element": {"element_type": 1}}}]})
    assert problem is not None
    assert problem.text("canvas_data") == (
        "canvas_data.nodes.0.data.element.element_type: must be a string"
    )
