"""Unit tests for tests/ai_eval/scoring.py: stack-free, all fakes.

Runs as part of the repo-root tests/unit/ suite (`uv run pytest tests/unit/
-v` from the repo root, no stack needed; wired into `make test` /
`make coverage` / `make coverage-parallel` via the `test-root` Makefile
target), unlike tests/ai_eval/test_generate_eval.py itself, which is a live,
opt-in suite (HERD_AI_EVAL=1) against a running stack.
"""

import json
from pathlib import Path
from typing import Any

from tests.ai_eval.scoring import build_canvas_data, classify_run, summarize_runs

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_PATH = REPO_ROOT / "tests" / "ai_eval" / "prompts.json"


def _resolved_device(role: str, device_id: str) -> dict[str, Any]:
    return {
        "role": role,
        "template_name": f"Template for {role}",
        "topology_type": "PHYSICAL",
        "device": {"id": device_id, "name": f"{role}-device"},
        "config": None,
    }


def _edge(source_role: str, target_role: str, layer: str = "L2") -> dict[str, Any]:
    return {"source_role": source_role, "target_role": target_role, "layer": layer}


# --- build_canvas_data --------------------------------------------------


def test_build_canvas_data_top_level_keys_match_committer():
    response = {
        "devices": [_resolved_device("router", "dev-1"), _resolved_device("switch", "dev-2")],
        "edges": [_edge("router", "switch")],
    }
    canvas = build_canvas_data(response)
    assert set(canvas.keys()) == {"nodes", "edges", "selectedEdgeLayer"}
    assert canvas["selectedEdgeLayer"] == "L2"
    assert len(canvas["nodes"]) == 2
    assert len(canvas["edges"]) == 1


def test_build_canvas_data_node_shape_matches_committer():
    response = {"devices": [_resolved_device("router", "dev-1")], "edges": []}
    canvas = build_canvas_data(response)
    node = canvas["nodes"][0]
    assert set(node.keys()) == {"id", "type", "position", "data"}
    assert node["type"] == "deviceNode"
    assert set(node["position"].keys()) == {"x", "y"}
    assert set(node["data"].keys()) == {"device", "label", "topologyType"}
    assert node["data"]["device"] == {"id": "dev-1"}
    assert node["data"]["label"] == "router"
    assert node["data"]["topologyType"] == "PHYSICAL"
    # A distinct, non-empty id per node (the committer uses uuid4; the exact
    # value does not matter, only that it is a usable, unique reference).
    assert isinstance(node["id"], str) and node["id"]


def test_build_canvas_data_edge_shape_matches_committer():
    response = {
        "devices": [_resolved_device("router", "dev-1"), _resolved_device("switch", "dev-2")],
        "edges": [_edge("router", "switch", layer="L1")],
    }
    canvas = build_canvas_data(response)
    edge = canvas["edges"][0]
    assert set(edge.keys()) == {"id", "source", "target", "data"}
    assert set(edge["data"].keys()) == {"layer"}
    assert edge["data"]["layer"] == "L1"
    node_ids = {n["id"] for n in canvas["nodes"]}
    assert edge["source"] in node_ids
    assert edge["target"] in node_ids
    assert edge["source"] != edge["target"]


def test_build_canvas_data_skips_element_edges_and_dangling_roles():
    """Elements never become nodes; an edge touching one, or an unknown role,

    is dropped rather than raising, mirroring the committer's own drop of
    anything it cannot resolve to a device-to-device pair.
    """
    response = {
        "devices": [_resolved_device("router", "dev-1")],
        "edges": [
            _edge("router", "vlan-segment-1"),  # element role: no element node exists here
            _edge("router", "no-such-role"),  # dangling role
        ],
        "elements": [
            {
                "role": "vlan-segment-1",
                "element_type": "vlan_segment",
                "label": "VLAN 10",
                "attrs": {},
            }
        ],
    }
    canvas = build_canvas_data(response)
    assert len(canvas["nodes"]) == 1
    assert canvas["edges"] == []


def test_build_canvas_data_raises_on_unresolved_device():
    response = {"devices": [{"role": "router", "device": None}], "edges": []}
    try:
        build_canvas_data(response)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unresolved device")


# --- classify_run ---------------------------------------------------------


def _canvas(n_devices: int, n_edges: int) -> dict[str, Any]:
    nodes = [{"type": "deviceNode", "id": f"n{i}"} for i in range(n_devices)]
    edges = [{"id": f"e{i}"} for i in range(n_edges)]
    return {"nodes": nodes, "edges": edges, "selectedEdgeLayer": "L2"}


def test_classify_run_all_valid_and_minimums_met():
    canvas = _canvas(n_devices=2, n_edges=1)
    validation = {"valid": True, "invalid_edges": [], "device_ids": [], "invalid_routes": []}
    record = classify_run(canvas, validation, min_devices=2, min_edges=1)
    assert record["passed"] is True
    assert record["invalid_edge_reasons"] == {}
    assert record["n_devices"] == 2
    assert record["n_edges"] == 1


def test_classify_run_no_path_fails():
    canvas = _canvas(n_devices=2, n_edges=1)
    validation = {
        "valid": False,
        "invalid_edges": [{"edge_id": "e0", "reason": "no_path"}],
        "device_ids": [],
        "invalid_routes": [],
    }
    record = classify_run(canvas, validation, min_devices=2, min_edges=1)
    assert record["passed"] is False
    assert record["invalid_edge_reasons"] == {"no_path": 1}


def test_classify_run_missing_device_fails():
    canvas = _canvas(n_devices=2, n_edges=1)
    validation = {
        "valid": False,
        "invalid_edges": [{"edge_id": "e0", "reason": "missing_device"}],
        "device_ids": [],
        "invalid_routes": [],
    }
    record = classify_run(canvas, validation, min_devices=2, min_edges=1)
    assert record["passed"] is False
    assert record["invalid_edge_reasons"] == {"missing_device": 1}


def test_classify_run_mixed_reasons_counted_separately():
    canvas = _canvas(n_devices=3, n_edges=2)
    validation = {
        "valid": False,
        "invalid_edges": [
            {"edge_id": "e0", "reason": "no_path"},
            {"edge_id": "e1", "reason": "no_path"},
            {"edge_id": "e2", "reason": "missing_device"},
        ],
        "device_ids": [],
        "invalid_routes": [],
    }
    record = classify_run(canvas, validation, min_devices=3, min_edges=2)
    assert record["passed"] is False
    assert record["invalid_edge_reasons"] == {"no_path": 2, "missing_device": 1}


def test_classify_run_below_minimum_devices_fails_even_with_no_invalid_edges():
    canvas = _canvas(n_devices=1, n_edges=0)
    validation = {"valid": True, "invalid_edges": [], "device_ids": [], "invalid_routes": []}
    record = classify_run(canvas, validation, min_devices=3, min_edges=2)
    assert record["passed"] is False
    assert record["n_devices"] == 1
    assert record["n_edges"] == 0
    assert record["invalid_edge_reasons"] == {}


def test_classify_run_below_minimum_edges_fails():
    canvas = _canvas(n_devices=3, n_edges=1)
    validation = {"valid": True, "invalid_edges": [], "device_ids": [], "invalid_routes": []}
    record = classify_run(canvas, validation, min_devices=3, min_edges=2)
    assert record["passed"] is False


# --- summarize_runs --------------------------------------------------------


def _run_record(
    *,
    http_status: int = 200,
    passed: bool = True,
    reasons=None,
    latency_s: float = 1.0,
    tokens=None,
) -> dict[str, Any]:
    return {
        "http_status": http_status,
        "passed": passed,
        "invalid_edge_reasons": reasons or {},
        "n_devices": 1,
        "n_edges": 1,
        "latency_s": latency_s,
        "output_tokens": tokens,
    }


def test_summarize_runs_empty_list():
    summary = summarize_runs([])
    assert summary == {
        "n": 0,
        "http_ok": 0,
        "passed": 0,
        "pass_rate_pct": 0.0,
        "reason_counts": {},
        "latency_s": {"p50": 0.0, "p95": 0.0, "max": 0.0},
        "output_tokens_median": None,
    }


def test_summarize_runs_pass_rate_and_reason_aggregation():
    records = [
        _run_record(passed=True),
        _run_record(passed=False, reasons={"no_path": 1}),
        _run_record(passed=False, reasons={"no_path": 1, "missing_device": 2}),
        _run_record(http_status=502, passed=False),
    ]
    summary = summarize_runs(records)
    assert summary["n"] == 4
    assert summary["http_ok"] == 3
    assert summary["passed"] == 1
    assert summary["pass_rate_pct"] == 25.0
    assert summary["reason_counts"] == {"no_path": 2, "missing_device": 2}


def test_summarize_runs_latency_percentiles_on_a_small_list():
    records = [_run_record(latency_s=v) for v in (1.0, 2.0, 3.0, 4.0, 5.0)]
    summary = summarize_runs(records)
    # Nearest-rank over 5 sorted values: p50 lands on the middle value (true
    # median here), p95 on the last (largest) value.
    assert summary["latency_s"] == {"p50": 3.0, "p95": 5.0, "max": 5.0}


def test_summarize_runs_output_tokens_median_when_present():
    records = [_run_record(tokens=t) for t in (100, 150, 200)]
    summary = summarize_runs(records)
    assert summary["output_tokens_median"] == 150


def test_summarize_runs_output_tokens_median_none_when_absent():
    records = [_run_record(tokens=None), _run_record(tokens=None)]
    summary = summarize_runs(records)
    assert summary["output_tokens_median"] is None


# --- prompts.json -----------------------------------------------------------


def test_prompts_json_parses_and_every_entry_has_the_four_fields():
    prompts = json.loads(PROMPTS_PATH.read_text())
    assert isinstance(prompts, list)
    assert len(prompts) >= 5
    seen_ids = set()
    for entry in prompts:
        assert isinstance(entry.get("id"), str) and entry["id"]
        assert entry["id"] not in seen_ids, f"duplicate prompt id: {entry['id']}"
        seen_ids.add(entry["id"])
        assert isinstance(entry.get("prompt"), str) and entry["prompt"]
        assert isinstance(entry.get("min_devices"), int) and entry["min_devices"] >= 1
        assert isinstance(entry.get("min_edges"), int) and entry["min_edges"] >= 0
