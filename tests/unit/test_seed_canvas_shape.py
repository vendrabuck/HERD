"""The seed data's canvases have the shape cabling holds every canvas to (TOPO-SHAPE-4).

`seedtools.topologies.build_canvas` builds every canvas `make seed` stores, through
the same topology PUT the editor uses, so a canvas it builds that the check refuses
would fail the seed. This calls the real builder, for each kind of node it writes,
and judges its output with cabling's own check, loaded by path: cabling's `app`
package is not importable from the repository root.
"""

import importlib.util
import sys
from pathlib import Path

from seedtools.topologies import build_canvas

REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECK_PATH = REPO_ROOT / "services" / "cabling" / "app" / "services" / "canvas_shape.py"


def _canvas_problem():
    name = "cabling_canvas_shape_for_seed_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _CHECK_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].canvas_problem


# An inventory device record as the seed reads it, with every field the builder
# projects, and one with the nullable ones null.
_DEVICE = {
    "id": "8d1e7a8e-4b8f-4b39-8d0a-1c5d2c8f6a01",
    "name": "sw-1",
    "topology_type": "PHYSICAL",
    "template_name": "Generic Switch",
    "template_icon": "switch",
    "status": "AVAILABLE",
    "connection_type": "Layer 3 Switch",
    "field_data": {"note": "not projected"},
}
_SPARSE_DEVICE = {
    "id": "9e2f8b9f-5c90-4c4a-9e1b-2d6e3d907b12",
    "name": None,
    "topology_type": None,
    "template_name": None,
    "template_icon": None,
    "status": None,
    "connection_type": None,
}


def test_every_kind_of_seed_node_and_edge_has_the_shape():
    thin = "0a3b4c5d-6e7f-4801-9a2b-3c4d5e6f7a8b"
    canvas = build_canvas(
        [None, thin, _DEVICE["id"], _SPARSE_DEVICE["id"]],
        [(0, 1, "L1"), (1, 2, "L2"), (2, 3, "L3")],
        {_DEVICE["id"]: _DEVICE, _SPARSE_DEVICE["id"]: _SPARSE_DEVICE},
    )
    # One node of each kind the builder writes: empty data, a thin reference, a
    # full device payload, and a payload built from a record with nulls.
    assert [sorted(node["data"]) for node in canvas["nodes"]] == [
        [],
        ["device"],
        ["device", "label", "topologyType"],
        ["device", "label", "topologyType"],
    ]
    assert len(canvas["edges"]) == 3
    assert _canvas_problem()(canvas) is None


def test_the_check_loaded_here_refuses_what_it_refuses_in_cabling():
    """The by-path load is the real check, not a stand-in that passes everything."""
    canvas = build_canvas([_DEVICE["id"]], [], {_DEVICE["id"]: {**_DEVICE, "status": 5}})
    problem = _canvas_problem()(canvas)
    assert problem is not None
    assert problem.text("canvas_data") == "canvas_data.nodes.0.data.device.status: must be a string"
