"""Unit tests for the one canvas shape check (`app.services.canvas_shape`).

The space is enumerated rather than sampled: every JSON type (an object, a list, a
string, an integer, a float, true, false, null, and the empty, zero, and false
forms) in every position the check names, the canvas itself included. The expected
answer for each comes from the table in this file, written from the module
docstring, not from the module's own tables, so a change to the module's tables
fails here.
"""

import asyncio
import copy
import json
import math
import threading

import pytest
from app.services import canvas_shape
from app.services.bulk_service import parse_csv_topologies
from app.services.canvas_shape import (
    INLINE_CHECK_VALUES,
    MAX_CANVAS_DEPTH,
    STORED_CANVAS_UNREADABLE_DETAIL,
    STORED_CHECK_THREADS,
    TEXT_CHARS_PER_VALUE,
    CanvasProblem,
    canvas_problem,
    readable_canvas_or_none,
    require_readable_canvas,
    stored_canvas_problem,
    stored_canvas_readable,
    stored_check_limiter,
    stored_shape_problem,
)
from fastapi import HTTPException
from herd_common import bulk_work
from herd_common.bulk_work import BULK_WORK_THREADS, bulk_work_limiter, run_bulk_work

OBJECT = "must be an object"
LIST = "must be a list"
STRING = "must be a string"

# One value of each JSON type, and the empty, zero, and false forms, keyed by a
# readable id.
VALUES = {
    "object": {},
    "object_with_a_key": {"k": "v"},
    "list": [],
    "string": "x",
    "empty_string": "",
    "int": 1,
    "zero": 0,
    "float": 1.5,
    "true": True,
    "false": False,
    "null": None,
}
_TYPE_OF = {
    "object": dict,
    "object_with_a_key": dict,
    "list": list,
    "string": str,
    "empty_string": str,
}

# Every position the check names, with the type it must have there.
POSITIONS = {
    ("nodes",): list,
    ("nodes", 0): dict,
    ("nodes", 0, "id"): str,
    ("nodes", 0, "data"): dict,
    ("nodes", 0, "data", "label"): str,
    ("nodes", 0, "data", "device"): dict,
    ("nodes", 0, "data", "device", "id"): str,
    ("nodes", 0, "data", "device", "name"): str,
    ("nodes", 0, "data", "device", "template_name"): str,
    ("nodes", 0, "data", "device", "role"): str,
    ("nodes", 0, "data", "device", "status"): str,
    ("nodes", 0, "data", "device", "topology_type"): str,
    ("nodes", 0, "data", "device", "template_icon"): str,
    ("nodes", 0, "data", "device", "connection_type"): str,
    ("nodes", 0, "data", "topologyType"): str,
    ("nodes", 0, "data", "element"): dict,
    ("nodes", 0, "data", "element", "id"): str,
    ("nodes", 0, "data", "element", "label"): str,
    ("nodes", 0, "data", "element", "element_type"): str,
    ("edges",): list,
    ("edges", 0): dict,
    ("edges", 0, "id"): str,
    ("edges", 0, "source"): str,
    ("edges", 0, "target"): str,
    ("edges", 0, "data"): dict,
    ("edges", 0, "data", "layer"): str,
    ("edges", 0, "data", "source_port_name"): str,
    ("edges", 0, "data", "target_port_name"): str,
    ("edges", 0, "data", "sourcePort"): str,
    ("edges", 0, "data", "targetPort"): str,
}
_MESSAGE = {dict: OBJECT, list: LIST, str: STRING}

# A list item may not be null: only a named field may.
_ITEM_POSITIONS = {("nodes", 0), ("edges", 0)}


def _base() -> dict:
    """A canvas with every named position present and of the right type."""
    return {
        "nodes": [
            {
                "id": "n1",
                "type": "deviceNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": "sw-1",
                    "device": {
                        "id": "8d1e7a8e-4b8f-4b39-8d0a-1c5d2c8f6a01",
                        "name": "sw-1",
                        "template_name": "Generic Switch",
                        "role": "switch-1",
                        "status": "AVAILABLE",
                        "topology_type": "PHYSICAL",
                        "template_icon": "switch",
                        "connection_type": "Layer 3 Switch",
                    },
                    "topologyType": "PHYSICAL",
                    "element": {"id": "e-1", "label": "VLAN 10", "element_type": "vlan_segment"},
                },
            }
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "n1",
                "data": {
                    "layer": "L1",
                    "source_port_name": "eth0",
                    "target_port_name": "eth1",
                    "sourcePort": "eth0",
                    "targetPort": "eth1",
                },
            }
        ],
    }


def _set(canvas: dict, path: tuple, value) -> dict:
    out = copy.deepcopy(canvas)
    parent = out
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    return out


def _cases():
    for path, expected in POSITIONS.items():
        for value_id, value in VALUES.items():
            if value is None:
                answer = None if path not in _ITEM_POSITIONS else OBJECT
            elif _TYPE_OF.get(value_id) is expected:
                answer = None
            else:
                answer = _MESSAGE[expected]
            yield pytest.param(
                path, value, answer, id=".".join(str(p) for p in path) + "=" + value_id
            )


def test_the_base_canvas_has_the_shape():
    assert canvas_problem(_base()) is None


@pytest.mark.parametrize(("path", "value", "message"), list(_cases()))
def test_each_json_type_in_each_position(path, value, message):
    canvas = _set(_base(), path, value)
    problem = canvas_problem(canvas)
    if message is None:
        assert problem is None
        return
    assert problem == CanvasProblem(path, message)
    assert problem.text("canvas_data") == (
        "canvas_data." + ".".join(str(p) for p in path) + ": " + message
    )


@pytest.mark.parametrize(
    "value_id", [k for k in VALUES if k not in ("object", "object_with_a_key")]
)
def test_the_canvas_itself_must_be_an_object(value_id):
    value = VALUES[value_id]
    if value is None:
        # No canvas at all is a canvas with nothing on it.
        assert canvas_problem(None) is None
        return
    problem = canvas_problem(value)
    assert problem == CanvasProblem((), OBJECT)
    assert problem.text("canvas_data") == "canvas_data: must be an object"


@pytest.mark.parametrize(
    "canvas",
    [{}, {"nodes": None, "edges": None}, {"nodes": [], "edges": []}, {"viewport": {"x": 1}}],
)
def test_an_empty_canvas_has_the_shape(canvas):
    assert canvas_problem(canvas) is None


def test_a_missing_parent_leaves_its_children_unjudged():
    """With `data` absent, null, or `{}`, nothing under it is judged, and with
    `data.device` absent the device's fields are not either."""
    for data in (None, {}):
        assert canvas_problem({"nodes": [{"id": "n1", "data": data}]}) is None
    assert canvas_problem({"nodes": [{"id": "n1"}]}) is None
    assert canvas_problem({"nodes": [{"data": {"label": "x"}}]}) is None


def test_a_list_item_must_be_an_object_whatever_it_is():
    for collection in ("nodes", "edges"):
        for value in VALUES.values():
            if isinstance(value, dict):
                continue
            problem = canvas_problem({collection: [value]})
            assert problem == CanvasProblem((collection, 0), OBJECT)


def test_the_first_problem_in_canvas_order_is_named():
    """Nodes before edges, items in order, a node's fields in the documented order
    (data before data.device before data.device.name), then the content walk."""
    canvas = {
        "nodes": [{"id": "n1"}, {"id": "n2", "data": {"device": {"name": 1}, "label": 2}}, 1],
        "edges": [1],
    }
    assert canvas_problem(canvas) == CanvasProblem(("nodes", 1, "data", "device", "name"), STRING)
    assert canvas_problem({"nodes": [{"data": 1, "id": 2}]}) == CanvasProblem(
        ("nodes", 0, "id"), STRING
    )
    # A structural problem anywhere is named before a content problem anywhere.
    assert canvas_problem({"nodes": [{"x": math.nan}], "edges": [1]}) == CanvasProblem(
        ("edges", 0), OBJECT
    )


# Bounds of the columns a canvas string is written to ------------------------------------


def test_an_edge_id_is_bounded_by_its_column():
    """A fork save writes each edge's `id` into `fork_connections.edge_key`, a
    String(255): 255 characters pass (a character outside the BMP counts as one,
    as the column counts it), 256 do not."""
    for edge_id in ("e" * 255, "\U0001f600" * 255, ""):
        assert canvas_problem({"edges": [{"id": edge_id}]}) is None
    problem = canvas_problem({"edges": [{"id": "marker-9f3c" + "e" * 245}]})
    assert problem == CanvasProblem(("edges", 0, "id"), "is longer than 255 characters")
    assert problem.text("canvas_data") == "canvas_data.edges.0.id: is longer than 255 characters"


@pytest.mark.parametrize("edge_id", ["\x00", "e\x00", "\x00e", "a\x00b"])
def test_an_edge_id_holding_a_nul_is_refused(edge_id):
    problem = canvas_problem({"edges": [{"id": edge_id}]})
    assert problem == CanvasProblem(
        ("edges", 0, "id"), "holds a NUL character, which no database column takes"
    )


def test_the_edge_id_is_judged_before_the_edge_ends():
    edge = {"source": 1, "id": "e" * 256}
    assert canvas_problem({"edges": [edge]}) == CanvasProblem(
        ("edges", 0, "id"), "is longer than 255 characters"
    )
    assert stored_shape_problem({"edges": [edge]}) == CanvasProblem(("edges", 0, "source"), STRING)


async def test_a_stored_edge_id_past_its_column_stays_readable():
    """The length bound is the write side's: a stored canvas with a longer edge id
    is read (and so repaired in the editor), while a NUL is refused on either side.
    A reader that writes the ids to the column judges it as written."""
    long_id = {"edges": [{"id": "e" * 256, "source": "n1", "target": "n2"}]}
    too_long = CanvasProblem(("edges", 0, "id"), "is longer than 255 characters")
    assert canvas_problem(long_id) == too_long
    assert stored_shape_problem(long_id) is None
    assert await stored_canvas_problem(long_id) is None
    assert await stored_canvas_problem(long_id, as_written=True) == too_long
    assert stored_canvas_readable(long_id, kind="topology", row_id=None) is True
    assert await require_readable_canvas(long_id, kind="topology", row_id=None) is long_id
    with pytest.raises(HTTPException) as exc:
        await require_readable_canvas(long_id, kind="fork_parent", row_id=None, as_written=True)
    assert (exc.value.status_code, exc.value.detail) == (409, STORED_CANVAS_UNREADABLE_DETAIL)
    # The same on a worker thread, for a canvas past the inline budget.
    wide = {"edges": [*long_id["edges"], *({} for _ in range(INLINE_CHECK_VALUES))]}
    assert await stored_canvas_problem(wide) is None
    assert await stored_canvas_problem(wide, as_written=True) == too_long
    holds_nul = CanvasProblem(
        ("edges", 0, "id"), "holds a NUL character, which no database column takes"
    )
    both = {"edges": [{"id": "\x00" + "e" * 300}]}
    assert canvas_problem(both) == too_long
    assert stored_shape_problem(both) == holds_nul
    assert await stored_canvas_problem(both) == holds_nul


# Depth ---------------------------------------------------------------------------


def _nested(levels: int, leaf=None) -> dict:
    """`levels` objects nested inside each other (the outermost is one level)."""
    value: object = leaf if leaf is not None else {}
    for _ in range(levels - 1):
        value = {"k": value}
    return value  # type: ignore[return-value]


def test_the_bound_is_what_the_docstring_says():
    assert MAX_CANVAS_DEPTH == 64


def test_a_node_at_the_bound_passes_and_one_past_it_is_refused():
    # The canvas is level 1, `nodes` 2, the node 3, `data` 4.
    at_bound = {"nodes": [{"id": "n1", "data": _nested(MAX_CANVAS_DEPTH - 3)}]}
    assert canvas_problem(at_bound) is None
    past = {"nodes": [{"id": "n1", "data": _nested(MAX_CANVAS_DEPTH - 2)}]}
    problem = canvas_problem(past)
    assert problem == CanvasProblem(("nodes", 0), "is nested deeper than 64 levels")
    assert problem.text("canvas_data") == "canvas_data.nodes.0: is nested deeper than 64 levels"


def test_lists_count_as_levels_like_objects():
    # The edge is level 3, so `extra` is level 4 and its innermost list level 64.
    value: object = []
    for _ in range(MAX_CANVAS_DEPTH - 4):
        value = [value]
    assert canvas_problem({"edges": [{"id": "e1", "extra": value}]}) is None
    assert canvas_problem({"edges": [{"id": "e1", "extra": [value]}]}) == CanvasProblem(
        ("edges", 0), "is nested deeper than 64 levels"
    )


def test_another_top_level_key_is_judged_and_never_named():
    """`viewport` is level 2; a key the caller chose is never in the path."""
    assert canvas_problem({"caller-chosen-key": _nested(MAX_CANVAS_DEPTH - 1)}) is None
    problem = canvas_problem({"caller-chosen-key": _nested(MAX_CANVAS_DEPTH)})
    assert problem == CanvasProblem((), "is nested deeper than 64 levels")
    assert "caller-chosen-key" not in problem.text("canvas_data")


def test_a_canvas_past_the_recursion_limit_is_judged_not_raised_on():
    """Two hundred thousand levels: far past every interpreter's recursion limit and
    the JSON encoder's, so a recursive check would raise."""
    deep = {"nodes": [{"id": "n1", "data": _nested(200_000)}]}
    with pytest.raises(RecursionError):
        json.dumps(deep)
    assert canvas_problem(deep) == CanvasProblem(("nodes", 0), "is nested deeper than 64 levels")


# Numbers and text --------------------------------------------------------------------


@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize(
    ("canvas_of", "path"),
    [
        (lambda v: {"nodes": [{"id": "n1", "position": {"x": v, "y": 0}}]}, ("nodes", 0)),
        (lambda v: {"edges": [{"id": "e1", "data": {"weight": [v]}}]}, ("edges", 0)),
        (lambda v: {"viewport": {"zoom": v}}, ()),
        (lambda v: {"zoom": v}, ()),
    ],
    ids=["node", "edge", "other_key", "top_level_value"],
)
def test_a_number_that_is_not_finite_is_refused(number, canvas_of, path):
    assert canvas_problem(canvas_of(number)) == CanvasProblem(
        path, "holds a number that is not finite"
    )


def test_finite_numbers_of_every_kind_pass():
    canvas = {"nodes": [{"id": "n1", "position": {"x": 1e308, "y": -0.0, "z": 10**30}}]}
    assert canvas_problem(canvas) is None


@pytest.mark.parametrize(
    ("canvas", "path"),
    [
        ({"nodes": [{"id": "n1", "data": {"label": "a\ud800b"}}]}, ("nodes", 0)),
        ({"nodes": [{"id": "n1", "data": {"attrs": {"k\udc00": 1}}}]}, ("nodes", 0)),
        ({"edges": [{"id": "e1", "data": {"note": ["\udfff"]}}]}, ("edges", 0)),
        ({"viewport": {"name": "\ud800"}}, ()),
        ({"key\ud800": 1}, ()),
    ],
    ids=["node_value", "node_key", "edge_value", "other_key_value", "top_level_key"],
)
def test_a_lone_surrogate_is_refused(canvas, path):
    message = "holds a lone surrogate escape, which is not valid text"
    assert canvas_problem(canvas) == CanvasProblem(path, message)


def test_a_character_outside_the_bmp_is_not_a_lone_surrogate():
    assert canvas_problem({"nodes": [{"id": "n1", "data": {"label": "\U0001f600"}}]}) is None


def test_no_message_holds_a_submitted_value():
    """Each refusal's text is built from the field names and indexes alone."""
    marker = "marker-value-9f3c"
    canvases = [
        {"nodes": [{"id": [marker]}]},
        {"nodes": [{"id": "n1", "data": marker}]},
        {"edges": [{"id": "e1", "source": {marker: marker}}]},
        {"nodes": [{"id": "n1", "data": {marker: _nested(80)}}]},
        {marker: {"x": math.nan}},
        {marker: "\ud800"},
    ]
    for canvas in canvases:
        problem = canvas_problem(canvas)
        assert problem is not None
        assert marker not in problem.text("canvas_data")


# What HERD itself writes passes --------------------------------------------------------


def test_the_editor_canvas_passes():
    """The shape `persistableCanvas` in TopologyEditorPage.tsx sends: React Flow nodes
    and edges, a device node with its allowlisted device and routing intent, an
    element node, an edge with chosen ports, and `selectedEdgeLayer`."""
    canvas = {
        "nodes": [
            {
                "id": "dnd-1",
                "type": "deviceNode",
                "position": {"x": 10.5, "y": 20},
                "measured": {"width": 180, "height": 60},
                "selected": False,
                "data": {
                    "device": {
                        "id": "8d1e7a8e-4b8f-4b39-8d0a-1c5d2c8f6a01",
                        "name": "l3-sw-1",
                        "topology_type": "PHYSICAL",
                        "connection_type": "Layer 3 Switch",
                        "status": "AVAILABLE",
                        "template_name": "Generic L3",
                        "template_icon": None,
                    },
                    "label": "l3-sw-1",
                    "topologyType": "PHYSICAL",
                    "l3": {
                        "routes": [
                            {
                                "destination": "10.0.0.0/24",
                                "next_hop": None,
                                "interface": "eth1",
                                "virtual_router": None,
                            }
                        ]
                    },
                },
            },
            {
                "id": "el-1",
                "type": "networkElementNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "element": {
                        "id": "4f1c",
                        "element_type": "vlan_segment",
                        "label": "VLAN 10",
                        "attrs": {"vlan": 10},
                    }
                },
            },
        ],
        "edges": [
            {
                "id": "xy-edge__dnd-1right-el-1left",
                "source": "dnd-1",
                "target": "el-1",
                "sourceHandle": "right",
                "targetHandle": None,
                "type": "layer",
                "data": {
                    "layer": "L2",
                    "source_port_id": "p-1",
                    "source_port_name": "eth1",
                    "isProposal": False,
                },
            }
        ],
        "selectedEdgeLayer": "L2",
    }
    assert canvas_problem(canvas) is None


def test_the_seed_and_ai_commit_canvases_pass():
    """Copies of what `seedtools/topologies.py`'s `build_canvas` and
    ai-orchestrator's `_build_canvas_data` write, one node of each kind; the real
    builders are judged in `tests/unit/test_seed_canvas_shape.py` and
    `services/ai-orchestrator/tests/test_committer_canvas_shape.py`."""
    seed = {
        "nodes": [
            {"id": "n0", "type": "deviceNode", "position": {"x": 100, "y": 100}, "data": {}},
            {
                "id": "n1",
                "type": "deviceNode",
                "position": {"x": 300, "y": 100},
                "data": {"device": {"id": "8d1e7a8e-4b8f-4b39-8d0a-1c5d2c8f6a01"}},
            },
            {
                "id": "n2",
                "type": "deviceNode",
                "position": {"x": 500, "y": 100},
                "data": {
                    "device": {"id": "x", "name": "", "template_name": None, "status": "AVAILABLE"},
                    "label": "",
                    "topologyType": "PHYSICAL",
                },
            },
        ],
        "edges": [
            {
                "id": "e0",
                "source": "n0",
                "target": "n1",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }
    ai = {
        "nodes": [
            {
                "id": "c1",
                "type": "networkElementNode",
                "position": {"x": 200, "y": 420},
                "data": {
                    "element": {
                        "id": "e",
                        "element_type": "subnet",
                        "label": "mgmt",
                        "attrs": {"cidr": "10.0.0.0/24", "tags": ["a", {"b": 1}]},
                    }
                },
            }
        ],
        "edges": [
            {
                "id": "x",
                "source": "c0",
                "target": "c1",
                "data": {"layer": "L2", "source_port_id": "p", "source_port_name": "eth0"},
            }
        ],
        "selectedEdgeLayer": "L2",
    }
    assert canvas_problem(seed) is None
    assert canvas_problem(ai) is None


def test_a_csv_import_record_passes():
    raw = (
        "topology_name,source_device,source_port,target_device,target_port,layer\n"
        "lab,sw-a,eth0,sw-b,,L1\n"
    ).encode()
    (record,) = parse_csv_topologies(raw)
    assert canvas_problem(record["canvas"]) is None


# The read-side helpers -------------------------------------------------------------------


async def test_require_readable_canvas_returns_a_good_canvas_and_refuses_a_bad_one(caplog):
    good = _base()
    assert await require_readable_canvas(good, kind="topology", row_id=None) is good
    assert await require_readable_canvas(None, kind="topology", row_id=None) is None
    with pytest.raises(HTTPException) as exc:
        await require_readable_canvas({"nodes": ["marker-9f3c"]}, kind="topology", row_id=None)
    assert exc.value.status_code == 409
    assert exc.value.detail == STORED_CANVAS_UNREADABLE_DETAIL
    assert exc.value.detail == (
        "Stored canvas cannot be read; save a new canvas to replace it, or delete it"
    )
    messages = [r.getMessage() for r in caplog.records if r.name == "app.services.canvas_shape"]
    assert messages == [
        "stored_canvas_unreadable kind=topology id=None problem=canvas_data.nodes.0: "
        "must be an object"
    ]
    assert all("marker-9f3c" not in m for m in messages)


async def test_readable_canvas_or_none_gives_none_for_a_bad_canvas(caplog):
    good = _base()
    assert await readable_canvas_or_none(good, kind="fork", row_id=None) is good
    assert await readable_canvas_or_none({"edges": "x"}, kind="fork", row_id=None) is None
    assert any(
        "stored_canvas_unreadable kind=fork" in r.getMessage()
        and "canvas_data.edges: must be a list" in r.getMessage()
        for r in caplog.records
    )


def _threads_of_the_check(monkeypatch) -> list[int]:
    """Record the thread each full read-side check (``stored_shape_problem``) runs
    on."""
    seen: list[int] = []
    real = canvas_shape.stored_shape_problem

    def recording(canvas):
        seen.append(threading.get_ident())
        return real(canvas)

    monkeypatch.setattr(canvas_shape, "stored_shape_problem", recording)
    return seen


async def test_a_stored_canvas_within_the_budget_is_judged_on_the_loop(monkeypatch):
    """The canvas's own entry and each list item cost one, so one entry holding
    `INLINE_CHECK_VALUES - 1` empty edges is exactly the budget: judged where the
    request runs, with no worker thread."""
    seen = _threads_of_the_check(monkeypatch)
    canvas = {"edges": [{} for _ in range(INLINE_CHECK_VALUES - 1)]}
    assert await stored_canvas_problem(canvas) is None
    assert seen == []
    bad = {"edges": [{} for _ in range(INLINE_CHECK_VALUES - 2)] + [1]}
    assert await stored_canvas_problem(bad) == CanvasProblem(
        ("edges", INLINE_CHECK_VALUES - 2), OBJECT
    )
    # An item costs one more for each of its keys, and a nested container one
    # plus one for each of its values: each canvas here is exactly the budget.
    one_wide_item = {"edges": [{f"k{i}": 0 for i in range(INLINE_CHECK_VALUES - 2)}]}
    assert await stored_canvas_problem(one_wide_item) is None
    nested = {"nodes": [{"data": {f"k{i}": 0 for i in range(INLINE_CHECK_VALUES - 4)}}]}
    assert await stored_canvas_problem(nested) is None
    wide_top_level = {f"k{i}": 0 for i in range(INLINE_CHECK_VALUES)}
    assert await stored_canvas_problem(wide_top_level) is None
    assert seen == []


async def test_a_stored_canvas_past_the_budget_is_judged_on_a_worker_thread(monkeypatch):
    """One value more and the whole check runs on a worker thread, with the same
    answer the check gives anywhere."""
    seen = _threads_of_the_check(monkeypatch)
    loop_thread = threading.get_ident()
    canvas = {"edges": [{} for _ in range(INLINE_CHECK_VALUES)]}
    assert await stored_canvas_problem(canvas) is None
    assert len(seen) == 1 and seen[0] != loop_thread
    bad = {"edges": [{} for _ in range(INLINE_CHECK_VALUES - 1)] + [{"id": 1}]}
    assert await stored_canvas_problem(bad) == CanvasProblem(
        ("edges", INLINE_CHECK_VALUES - 1, "id"), STRING
    )
    assert len(seen) == 2 and seen[1] != loop_thread
    # Each of the exact-budget canvases of the test above, one value larger.
    one_wide_item = {"edges": [{f"k{i}": 0 for i in range(INLINE_CHECK_VALUES - 1)}]}
    assert await stored_canvas_problem(one_wide_item) is None
    nested = {"nodes": [{"data": {f"k{i}": 0 for i in range(INLINE_CHECK_VALUES - 3)}}]}
    assert await stored_canvas_problem(nested) is None
    assert len(seen) == 4 and loop_thread not in seen


async def test_a_wide_top_level_is_judged_on_a_worker_thread(monkeypatch):
    """Every entry of the canvas itself costs one, scalar or not, so a canvas of
    many top-level keys is not judged whole on the loop; a bad value past the
    budget is still found."""
    seen = _threads_of_the_check(monkeypatch)
    wide = {f"k{i}": 0 for i in range(INLINE_CHECK_VALUES + 1)}
    assert await stored_canvas_problem(wide) is None
    assert len(seen) == 1
    wide[f"k{INLINE_CHECK_VALUES + 1}"] = math.inf
    assert await stored_canvas_problem(wide) == CanvasProblem(
        (), "holds a number that is not finite"
    )
    assert len(seen) == 2 and threading.get_ident() not in seen


class _Text(str):
    """A ``str`` subclass, which only a Python caller builds, judged on the check's
    slower path."""


# Every place the check searches a string character by character, with what the
# canvas costs besides that string's characters, and how many times the string is
# searched (an edge id is searched for a NUL, and one that is not ASCII for a lone
# surrogate as well). `TEXT_CHARS_PER_VALUE` characters cost one value each time.
TEXT_POSITIONS = {
    "top_level_value": (lambda s: {"k": s}, 1, 1),
    "top_level_key": (lambda s: {s: 0}, 1, 1),
    "top_level_nested_value": (lambda s: {"k": {"v": s}}, 3, 1),
    "top_level_nested_key": (lambda s: {"k": {s: 0}}, 3, 1),
    "node_value": (lambda s: {"nodes": [{"v": s}]}, 3, 1),
    "node_key": (lambda s: {"nodes": [{s: 0}]}, 3, 1),
    "node_label": (lambda s: {"nodes": [{"data": {"label": s}}]}, 5, 1),
    "nested_key": (lambda s: {"nodes": [{"d": {s: 0}}]}, 5, 1),
    "nested_list_item": (lambda s: {"nodes": [{"d": [s]}]}, 5, 1),
    "edge_value": (lambda s: {"edges": [{"v": s}]}, 3, 1),
    "edge_id_ascii": (lambda s: {"edges": [{"id": s.replace("\u00e9", "e")}]}, 3, 1),
    "edge_id_not_ascii": (lambda s: {"edges": [{"id": s}]}, 3, 2),
    "subclass_top_level": (lambda s: {"k": _Text(s)}, 1, 1),
    "subclass_node_value": (lambda s: {"nodes": [{"v": _Text(s)}]}, 3, 1),
    "subclass_nested_value": (lambda s: {"nodes": [{"d": {"v": _Text(s)}}]}, 5, 1),
}


@pytest.mark.parametrize("position", list(TEXT_POSITIONS))
async def test_a_long_string_is_charged_by_its_length(monkeypatch, position):
    """A string that is not ASCII, and an edge id, costs one more value for every
    `TEXT_CHARS_PER_VALUE` characters, wherever it sits: the longest one the budget
    holds is judged on the loop, one character more on a worker thread."""
    assert TEXT_CHARS_PER_VALUE == 16
    build, base, searches = TEXT_POSITIONS[position]
    seen = _threads_of_the_check(monkeypatch)
    fits = ((INLINE_CHECK_VALUES - base) // searches) * 16 + 15
    assert await stored_canvas_problem(build("\u00e9" * fits)) is None
    assert seen == []
    assert await stored_canvas_problem(build("\u00e9" * (fits + 1))) is None
    assert len(seen) == 1 and seen[0] != threading.get_ident()


# The stored check's own worker threads ----------------------------------------------------
#
# Every overlap is staged with events, never a sleep: a check blocks on a
# `threading.Event` the test sets, and the test waits for a check's signal off the
# loop. Every wait, the final gathers included, is bounded and fails the test when
# it expires, so a slot that is never given back fails rather than hangs.

_WAIT = 10.0

# A canvas past the inline budget, so its check always runs on a worker thread.
_PAST_THE_BUDGET = {"edges": [{} for _ in range(INLINE_CHECK_VALUES)]}


async def _signalled(event: threading.Event) -> bool:
    return await asyncio.to_thread(event.wait, _WAIT)


async def _until(predicate) -> None:
    """Yield to the loop until ``predicate()`` holds, failing after ``_WAIT``."""
    async with asyncio.timeout(_WAIT):
        while not predicate():
            await asyncio.sleep(0)


@pytest.fixture
def fresh_limiters(monkeypatch):
    """Limiters of this test's own, so a slot one test holds never reaches another."""
    monkeypatch.setattr(canvas_shape, "_check_slots", None)
    monkeypatch.setattr(canvas_shape, "_check_workers", None)
    monkeypatch.setattr(bulk_work, "_limiter", None)
    monkeypatch.setattr(bulk_work, "_workers", None)


def _held_checks(monkeypatch, count: int):
    """Make each worker-thread check signal its entry and block until released."""
    entered = [threading.Event() for _ in range(count)]
    release = threading.Event()
    calls: list[int] = []

    def held(canvas):
        index = len(calls)
        calls.append(index)
        entered[index].set()
        assert release.wait(_WAIT), "never released"
        return None

    monkeypatch.setattr(canvas_shape, "stored_shape_problem", held)
    return entered, release, calls


def test_two_check_threads_at_most():
    assert STORED_CHECK_THREADS == 2


@pytest.mark.usefixtures("fresh_limiters")
async def test_at_most_two_checks_run_at_once_on_their_own_slots(monkeypatch):
    """A third check waits for one of the check's own slots, and the bulk slots
    are never taken. Mutation-checked: judging on `run_bulk_work` fails this."""
    entered, release, _ = _held_checks(monkeypatch, STORED_CHECK_THREADS + 1)
    tasks = [
        asyncio.create_task(stored_canvas_problem(_PAST_THE_BUDGET))
        for _ in range(STORED_CHECK_THREADS + 1)
    ]
    try:
        for index in range(STORED_CHECK_THREADS):
            assert await _signalled(entered[index]), f"check {index} never started"
        limiter = stored_check_limiter()
        await _until(lambda: limiter.statistics().tasks_waiting == 1)
        assert limiter.borrowed_tokens == STORED_CHECK_THREADS
        assert not entered[STORED_CHECK_THREADS].is_set()
        assert bulk_work_limiter().borrowed_tokens == 0
    finally:
        release.set()
    async with asyncio.timeout(_WAIT):
        assert await asyncio.gather(*tasks) == [None] * len(tasks)
    assert entered[STORED_CHECK_THREADS].is_set()
    assert stored_check_limiter().borrowed_tokens == 0


@pytest.mark.usefixtures("fresh_limiters")
async def test_a_check_never_waits_behind_bulk_work():
    """With every bulk slot held by an import or an export, a stored canvas past
    the budget is still judged, with its answer."""
    entered = [threading.Event() for _ in range(BULK_WORK_THREADS)]
    release = threading.Event()

    def bulk(index):
        entered[index].set()
        assert release.wait(_WAIT), "never released"

    holders = [asyncio.create_task(run_bulk_work(bulk, i)) for i in range(BULK_WORK_THREADS)]
    try:
        for event in entered:
            assert await _signalled(event), "a bulk holder never started"
        bad = {"edges": [*_PAST_THE_BUDGET["edges"], 1]}
        async with asyncio.timeout(_WAIT):
            assert await stored_canvas_problem(_PAST_THE_BUDGET) is None
            assert await stored_canvas_problem(bad) == CanvasProblem(
                ("edges", INLINE_CHECK_VALUES), OBJECT
            )
        assert bulk_work_limiter().borrowed_tokens == BULK_WORK_THREADS
    finally:
        release.set()
    async with asyncio.timeout(_WAIT):
        await asyncio.gather(*holders)


@pytest.mark.usefixtures("fresh_limiters")
async def test_a_check_cancelled_while_it_waits_takes_no_slot_and_runs_nothing(monkeypatch):
    entered, release, calls = _held_checks(monkeypatch, STORED_CHECK_THREADS + 1)
    holders = [
        asyncio.create_task(stored_canvas_problem(_PAST_THE_BUDGET))
        for _ in range(STORED_CHECK_THREADS)
    ]
    try:
        for index in range(STORED_CHECK_THREADS):
            assert await _signalled(entered[index]), "a holder never started"
        waiter = asyncio.create_task(stored_canvas_problem(_PAST_THE_BUDGET))
        limiter = stored_check_limiter()
        await _until(lambda: limiter.statistics().tasks_waiting == 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert limiter.statistics().tasks_waiting == 0
        assert limiter.borrowed_tokens == STORED_CHECK_THREADS
    finally:
        release.set()
    async with asyncio.timeout(_WAIT):
        await asyncio.gather(*holders)
    await _until(lambda: stored_check_limiter().borrowed_tokens == 0)
    assert len(calls) == STORED_CHECK_THREADS


@pytest.mark.usefixtures("fresh_limiters")
async def test_a_cancelled_check_keeps_its_slot_until_its_thread_ends(monkeypatch):
    """The caller sees its cancellation at once; the slot comes back only when the
    thread ends, so the bound holds whatever is cancelled."""
    entered, release, _ = _held_checks(monkeypatch, 1)
    task = asyncio.create_task(stored_canvas_problem(_PAST_THE_BUDGET))
    try:
        assert await _signalled(entered[0]), "the check never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stored_check_limiter().borrowed_tokens == 1
    finally:
        release.set()
    await _until(lambda: stored_check_limiter().borrowed_tokens == 0)


@pytest.mark.usefixtures("fresh_limiters")
async def test_a_check_raising_on_its_thread_gives_its_slot_back(monkeypatch):
    def failing(canvas):
        raise RuntimeError("check failed")

    monkeypatch.setattr(canvas_shape, "stored_shape_problem", failing)
    with pytest.raises(RuntimeError):
        async with asyncio.timeout(_WAIT):
            await stored_canvas_problem(_PAST_THE_BUDGET)
    await _until(lambda: stored_check_limiter().borrowed_tokens == 0)


async def test_the_check_limiter_is_one_per_process(monkeypatch):
    monkeypatch.setattr(canvas_shape, "_check_slots", None)
    first = stored_check_limiter()
    assert stored_check_limiter() is first
    assert first.total_tokens == STORED_CHECK_THREADS
    assert first is not bulk_work_limiter()
