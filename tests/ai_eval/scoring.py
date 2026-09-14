"""Pure scoring helpers for the AI topology-generator evaluation harness.

No HTTP and no pytest import here on purpose: this module is exercised both by
the live suite (test_generate_eval.py, opt-in, needs a running stack) and by
tests/unit/test_ai_eval_scoring.py (stack-free, runs in CI) against fakes. Keep
it that way so the scoring math itself is tested without ever touching a
network.

build_canvas_data mirrors, for device nodes and device-to-device edges, the
exact shapes services/ai-orchestrator/app/services/committer.py's
_build_canvas_data writes (read that function before changing this one). It
deliberately does not reproduce the network-element side of that function
(the `networkElementNode` nodes and the device-to-element port-attachment
edges): an element edge never becomes a hop (see the network-elements
invariant in the repo's architecture notes), so it never touches cabling's
pathfinding and is out of scope for a wireability measurement. Any proposed
edge naming an element role on either end is silently skipped, matching how
such an edge is dropped whenever no element node exists to resolve it.

classify_run mirrors services/cabling/app/schemas/topology.py's
TopologyValidationResponse: it reads `invalid_edges` (each an InvalidEdge with
a `reason`) off the validate response and judges pass/fail against the
prompt's stated minimums. `invalid_routes` (the ADR 0014 L3 routing-intent
list) is not consulted: the generator never proposes L3 route intent, so a
canvas built by build_canvas_data can only ever carry L1 wiring.
"""

import uuid
from collections import Counter
from typing import Any

# The committer's layout constants (services/ai-orchestrator/app/services/
# committer.py::_build_canvas_data). Kept identical so a diff between the two
# files stays a diff of behavior, not of arbitrary numbers.
_BASE_X = 200
_BASE_Y = 200
_STEP_X = 220


def build_canvas_data(response: dict[str, Any]) -> dict[str, Any]:
    """Build a canvas_data dict from a GenerateResponse-shaped dict.

    `response` is the parsed JSON body of a 200 from POST /api/ai/generate,
    i.e. it has `devices` (each a dict with `role` and an already-resolved
    `device` dict carrying at least `id`, per generator.py's
    `_resolve_devices`) and `edges` (each a dict with `source_role`,
    `target_role`, and `layer`). `elements`, when present, is ignored (see the
    module docstring).

    Raises ValueError if a device entry has no resolved `device` dict: that
    is a precondition violation (an unresolved proposal is not something the
    committer's canvas builder could ever be handed either), not a
    wireability finding, so it is not folded into a passed/failed run record.
    """
    device_node_id_by_role: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []

    for idx, proposed in enumerate(response.get("devices", [])):
        device = proposed.get("device")
        if not device or not device.get("id"):
            raise ValueError(
                f"device role {proposed.get('role')!r} has no resolved device; "
                "build_canvas_data expects a resolved GenerateResponse"
            )
        node_id = str(uuid.uuid4())
        device_node_id_by_role[proposed["role"]] = node_id
        position = proposed.get("position") or {"x": _BASE_X + idx * _STEP_X, "y": _BASE_Y}
        nodes.append(
            {
                "id": node_id,
                "type": "deviceNode",
                "position": position,
                "data": {
                    "device": {"id": device["id"]},
                    "label": proposed["role"],
                    "topologyType": "PHYSICAL",
                },
            }
        )

    edges: list[dict[str, Any]] = []
    for edge in response.get("edges", []):
        source_role = edge.get("source_role")
        target_role = edge.get("target_role")
        if source_role not in device_node_id_by_role or target_role not in device_node_id_by_role:
            # Dangling role, or one/both ends are a network element: skipped,
            # matching the committer's own drop of anything it cannot resolve
            # to a device-to-device pair.
            continue
        edges.append(
            {
                "id": str(uuid.uuid4()),
                "source": device_node_id_by_role[source_role],
                "target": device_node_id_by_role[target_role],
                "data": {"layer": edge.get("layer", "L2")},
            }
        )

    return {"nodes": nodes, "edges": edges, "selectedEdgeLayer": "L2"}


def classify_run(
    canvas_data: dict[str, Any],
    validation_response: dict[str, Any],
    min_devices: int,
    min_edges: int,
) -> dict[str, Any]:
    """Classify one cabling validate response against a prompt's minimums.

    `validation_response` is the parsed JSON body of a 200 from POST
    /api/cabling/topologies/{id}/validate (cabling's TopologyValidationResponse):
    `invalid_edges` is a list of InvalidEdge dicts, each carrying a `reason`
    (missing_device, no_path, element_to_element, element_edge_no_port; the
    last two cannot occur here since build_canvas_data never emits an element
    edge). Passing means the model's proposal was both fully wireable (no
    invalid edges) AND actually matched the ask (met the prompt's device and
    edge count floors); a proposal that wires cleanly but only names one
    device for a "two firewalls" prompt is not a pass.
    """
    invalid_edges = validation_response.get("invalid_edges", [])
    n_devices = sum(1 for node in canvas_data.get("nodes", []) if node.get("type") == "deviceNode")
    n_edges = len(canvas_data.get("edges", []))
    passed = len(invalid_edges) == 0 and n_devices >= min_devices and n_edges >= min_edges
    return {
        "passed": passed,
        "invalid_edge_reasons": Counter(e.get("reason", "unknown") for e in invalid_edges),
        "n_devices": n_devices,
        "n_edges": n_edges,
    }


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile over a small sample; 0.0 for an empty input.

    A full interpolated-percentile implementation is not worth the code for
    the sample sizes this harness produces (HERD_AI_EVAL_N defaults to 3 runs
    per prompt); nearest-rank is deterministic and easy to hand-check in a
    unit test, which matters more here than statistical precision.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((pct / 100) * (len(ordered) - 1))
    index = max(0, min(len(ordered) - 1, index))
    return ordered[index]


def summarize_runs(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a list of per-run records into one report.

    Each record is expected to carry, at minimum: `http_status` (200 on a
    successful generate call, else the status code or None on a transport
    failure), `passed` (bool), `invalid_edge_reasons` (a Counter or plain
    dict of reason to count), `latency_s` (float, wall-clock time for the
    generate call), and optionally `output_tokens` (an int, or absent/None
    when the caller could not determine it; GenerateResponse carries no usage
    field, so the live suite always leaves this absent, but the summary
    treats it as optional for whatever other caller supplies it).
    """
    n = len(records)
    http_ok = sum(1 for r in records if r.get("http_status") == 200)
    passed = sum(1 for r in records if r.get("passed"))
    pass_rate_pct = round(100 * passed / n, 1) if n else 0.0

    reason_counts: Counter[str] = Counter()
    for r in records:
        reason_counts.update(r.get("invalid_edge_reasons") or {})

    latencies = [r["latency_s"] for r in records if r.get("latency_s") is not None]
    latency_summary = {
        "p50": _percentile(latencies, 50),
        "p95": _percentile(latencies, 95),
        "max": max(latencies) if latencies else 0.0,
    }

    tokens = [r["output_tokens"] for r in records if r.get("output_tokens") is not None]
    output_tokens_median = _percentile(tokens, 50) if tokens else None

    return {
        "n": n,
        "http_ok": http_ok,
        "passed": passed,
        "pass_rate_pct": pass_rate_pct,
        "reason_counts": reason_counts,
        "latency_s": latency_summary,
        "output_tokens_median": output_tokens_median,
    }
