import { describe, expect, it } from "vitest";

import { buildForkDiffOverlayCanvas, diffForkCanvases, edgeIdentityKey } from "@/lib/forkDiff";
import type { CanvasData, L3RouteIntent } from "@/types/topology.types";

function node(id: string, label = id): CanvasData["nodes"][number] {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: { device: { id: `dev-${id}`, name: label }, label, topologyType: "PHYSICAL" },
  } as unknown as CanvasData["nodes"][number];
}

function l3Node(id: string, routes: L3RouteIntent[], label = id): CanvasData["nodes"][number] {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: `dev-${id}`, name: label },
      label,
      topologyType: "PHYSICAL",
      l3: { routes },
    },
  } as unknown as CanvasData["nodes"][number];
}

function route(overrides: Partial<L3RouteIntent> = {}): L3RouteIntent {
  return {
    destination: "10.0.0.0/24",
    next_hop: null,
    interface: "eth0",
    virtual_router: null,
    ...overrides,
  };
}

function edge(
  id: string,
  source: string,
  target: string,
  sourcePortName: string,
  targetPortName: string,
): CanvasData["edges"][number] {
  return {
    id,
    source,
    target,
    type: "layerEdge",
    data: {
      layer: "L1",
      source_port_name: sourcePortName,
      target_port_name: targetPortName,
    },
  } as unknown as CanvasData["edges"][number];
}

function canvas(
  nodes: CanvasData["nodes"],
  edges: CanvasData["edges"],
): CanvasData {
  return { nodes, edges, selectedEdgeLayer: "L1" };
}

describe("diffForkCanvases", () => {
  it("reports no differences between identical canvases", () => {
    const before = canvas([node("n1"), node("n2")], [edge("e1", "n1", "n2", "eth1", "eth2")]);
    const after = canvas([node("n1"), node("n2")], [edge("e1", "n1", "n2", "eth1", "eth2")]);
    const diff = diffForkCanvases(before, after);
    expect(diff.addedNodes).toEqual([]);
    expect(diff.removedNodes).toEqual([]);
    expect(diff.addedEdges).toEqual([]);
    expect(diff.removedEdges).toEqual([]);
  });

  it("reports an added edge", () => {
    const before = canvas([node("n1"), node("n2")], [edge("e1", "n1", "n2", "eth1", "eth2")]);
    const after = canvas(
      [node("n1"), node("n2")],
      [edge("e1", "n1", "n2", "eth1", "eth2"), edge("e2", "n1", "n2", "eth3", "eth4")],
    );
    const diff = diffForkCanvases(before, after);
    expect(diff.addedEdges).toHaveLength(1);
    expect(diff.addedEdges[0].id).toBe("e2");
    expect(diff.removedEdges).toEqual([]);
    expect(diff.addedNodes).toEqual([]);
    expect(diff.removedNodes).toEqual([]);
  });

  it("reports a removed node (and its now-dangling edge as removed too)", () => {
    const before = canvas(
      [node("n1"), node("n2")],
      [edge("e1", "n1", "n2", "eth1", "eth2")],
    );
    const after = canvas([node("n1")], []);
    const diff = diffForkCanvases(before, after);
    expect(diff.removedNodes).toHaveLength(1);
    expect(diff.removedNodes[0].id).toBe("n2");
    expect(diff.addedNodes).toEqual([]);
    expect(diff.removedEdges).toHaveLength(1);
    expect(diff.removedEdges[0].id).toBe("e1");
  });

  it("does not report churn for the same wire re-drawn under a new edge id", () => {
    // Deleting an edge and redrawing the identical wire re-mints a fresh
    // genId() for the edge itself (see TopologyEditorPage/WiringDialog); the
    // underlying wire (same nodes, same port names) is unchanged.
    const before = canvas([node("n1"), node("n2")], [edge("e1", "n1", "n2", "eth1", "eth2")]);
    const after = canvas([node("n1"), node("n2")], [edge("e2-new-id", "n1", "n2", "eth1", "eth2")]);
    const diff = diffForkCanvases(before, after);
    expect(diff.addedEdges).toEqual([]);
    expect(diff.removedEdges).toEqual([]);
  });

  it("uses multiset semantics for same-key edges, not set membership (coordinator review)", () => {
    // Two edges between the same pair with no recorded port names (pre-#531
    // canvases stored no source_port_name/target_port_name) share the exact
    // same identity key. A plain Set of keys would collapse them into one
    // membership test and report zero change when only one is removed;
    // per-key COUNTS must catch it instead.
    const dupBefore = canvas(
      [node("n1"), node("n2")],
      [edge("e1", "n1", "n2", "", ""), edge("e2", "n1", "n2", "", "")],
    );
    const dupAfterOne = canvas([node("n1"), node("n2")], [edge("e1", "n1", "n2", "", "")]);

    const shrunk = diffForkCanvases(dupBefore, dupAfterOne);
    expect(shrunk.removedEdges).toHaveLength(1);
    expect(shrunk.addedEdges).toEqual([]);

    // The reverse: one same-key edge duplicating to two reports one added.
    const grown = diffForkCanvases(dupAfterOne, dupBefore);
    expect(grown.addedEdges).toHaveLength(1);
    expect(grown.removedEdges).toEqual([]);
  });

  it("treats a null/undefined canvas as empty rather than throwing", () => {
    const after = canvas([node("n1")], [edge("e1", "n1", "n1", "eth1", "eth2")]);
    expect(diffForkCanvases(null, after).addedNodes).toHaveLength(1);
    expect(diffForkCanvases(undefined, undefined).addedNodes).toEqual([]);
    expect(diffForkCanvases(after, null).removedNodes).toHaveLength(1);
  });
});

describe("edgeIdentityKey", () => {
  it("ignores the edge's own id", () => {
    const a = edge("e1", "n1", "n2", "eth1", "eth2");
    const b = edge("e2", "n1", "n2", "eth1", "eth2");
    expect(edgeIdentityKey(a)).toBe(edgeIdentityKey(b));
  });

  it("differs when the port names differ", () => {
    const a = edge("e1", "n1", "n2", "eth1", "eth2");
    const b = edge("e1", "n1", "n2", "eth9", "eth2");
    expect(edgeIdentityKey(a)).not.toBe(edgeIdentityKey(b));
  });
});

describe("buildForkDiffOverlayCanvas", () => {
  it("marks an added edge and keeps the compare side's nodes", () => {
    const compare = canvas(
      [node("n1"), node("n2")],
      [edge("e1", "n1", "n2", "eth1", "eth2")],
    );
    const diff = diffForkCanvases(canvas([node("n1"), node("n2")], []), compare);
    const overlay = buildForkDiffOverlayCanvas(compare, diff);
    expect(overlay.nodes).toHaveLength(2);
    const overlaidEdge = overlay.edges.find((e) => e.id === "e1");
    expect((overlaidEdge?.data as { diffStatus?: string })?.diffStatus).toBe("added");
  });

  it("synthesizes a removed edge back in when both endpoints survive", () => {
    const before = canvas([node("n1"), node("n2")], [edge("e1", "n1", "n2", "eth1", "eth2")]);
    const compare = canvas([node("n1"), node("n2")], []);
    const diff = diffForkCanvases(before, compare);
    const overlay = buildForkDiffOverlayCanvas(compare, diff);
    const ghost = overlay.edges.find((e) => e.id === "diff-removed-e1");
    expect(ghost).toBeDefined();
    expect((ghost?.data as { diffStatus?: string })?.diffStatus).toBe("removed");
  });

  it("does not synthesize a removed edge whose endpoint node is also gone", () => {
    const before = canvas(
      [node("n1"), node("n2")],
      [edge("e1", "n1", "n2", "eth1", "eth2")],
    );
    const compare = canvas([node("n1")], []); // n2 removed too
    const diff = diffForkCanvases(before, compare);
    const overlay = buildForkDiffOverlayCanvas(compare, diff);
    expect(overlay.edges.find((e) => e.id === "diff-removed-e1")).toBeUndefined();
  });
});

describe("diffForkCanvases: routingChangedNodes (E6, issue #34)", () => {
  it("reports no routing change for identical route sets in a different order", () => {
    const before = canvas(
      [l3Node("n1", [route({ destination: "10.0.0.0/24" }), route({ interface: "eth1" })])],
      [],
    );
    const after = canvas(
      [l3Node("n1", [route({ interface: "eth1" }), route({ destination: "10.0.0.0/24" })])],
      [],
    );
    expect(diffForkCanvases(before, after).routingChangedNodes).toEqual([]);
  });

  it("reports an added and a removed route on the same node", () => {
    const before = canvas([l3Node("n1", [route({ interface: "eth0" })])], []);
    const after = canvas([l3Node("n1", [route({ interface: "eth1" })])], []);
    const diff = diffForkCanvases(before, after);
    expect(diff.routingChangedNodes).toHaveLength(1);
    expect(diff.routingChangedNodes[0].node.id).toBe("n1");
    expect(diff.routingChangedNodes[0].added).toBe(1);
    expect(diff.routingChangedNodes[0].removed).toBe(1);
  });

  // Review fix F7: a client-side destination canonicalizer briefly lived in
  // lib/forkDiff.ts to make THIS case report no change; it was removed
  // because its premise was wrong (the server canonicalizes only into
  // fork_l3_routes, never back into canvas_data, so both canvases always
  // hold the user's own raw text and there is no canonicalization-only
  // difference to hide). Pinning the opposite now: two differently-written
  // but "same" destinations DO report a change, the same way edgeIdentityKey
  // diffs raw port names.
  it("reports a change when the destination text differs, even if it would canonicalize to the same network", () => {
    const before = canvas([l3Node("n1", [route({ destination: "10.0.0.5/24" })])], []);
    const after = canvas([l3Node("n1", [route({ destination: "10.0.0.0/24" })])], []);
    const diff = diffForkCanvases(before, after);
    expect(diff.routingChangedNodes).toHaveLength(1);
    expect(diff.routingChangedNodes[0].added).toBe(1);
    expect(diff.routingChangedNodes[0].removed).toBe(1);
  });

  it("does not report a node present on only one side (covered by added/removedNodes instead)", () => {
    const before = canvas([node("n1")], []);
    const after = canvas([node("n1"), l3Node("n2", [route()])], []);
    const diff = diffForkCanvases(before, after);
    expect(diff.routingChangedNodes).toEqual([]);
    expect(diff.addedNodes.map((n) => n.id)).toEqual(["n2"]);
  });

  it("distinguishes routes that differ only by virtual_router", () => {
    const before = canvas([l3Node("n1", [route({ virtual_router: "vr1" })])], []);
    const after = canvas([l3Node("n1", [route({ virtual_router: "vr2" })])], []);
    const diff = diffForkCanvases(before, after);
    expect(diff.routingChangedNodes).toHaveLength(1);
    expect(diff.routingChangedNodes[0].added).toBe(1);
    expect(diff.routingChangedNodes[0].removed).toBe(1);
  });
});
