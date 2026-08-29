import { describe, it, expect } from "vitest";

import {
  groupEdgesForRender,
  isAnnotationEdge,
} from "@/components/topology-editor/edges/groupEdgesForRender";
import type { LayerEdgeData } from "@/types/topology.types";
import type { Edge } from "@xyflow/react";

function edge(id: string, data: Partial<LayerEdgeData> = {}): Edge<LayerEdgeData> {
  return {
    id,
    source: "a",
    target: "b",
    data: { layer: "L1", ...data } as LayerEdgeData,
  };
}

describe("isAnnotationEdge", () => {
  it("is false for a plain committed-wiring edge", () => {
    expect(isAnnotationEdge({ layer: "L1" } as LayerEdgeData)).toBe(false);
  });

  it("is true for an AI ghost proposal edge", () => {
    expect(isAnnotationEdge({ layer: "L1", isProposal: true } as LayerEdgeData)).toBe(true);
  });

  it("is true for a fork version diff overlay edge", () => {
    expect(isAnnotationEdge({ layer: "L1", diffStatus: "added" } as LayerEdgeData)).toBe(true);
  });

  it("is false for undefined edge data", () => {
    expect(isAnnotationEdge(undefined)).toBe(false);
  });
});

describe("groupEdgesForRender annotation exclusion", () => {
  it("excludes a diff overlay edge from bundling even when it shares a pair with another diff edge", () => {
    const edges = [
      edge("e1", { diffStatus: "added" }),
      edge("e2", { diffStatus: "removed" }),
    ];
    const { renderEdges, bundleMembers } = groupEdgesForRender(edges);
    // Both diff edges pass through individually; neither collapses into a
    // bundledEdge, since a diff overlay is a read-only annotation, not a
    // duplicate wire.
    expect(renderEdges).toHaveLength(2);
    expect(bundleMembers.size).toBe(0);
  });

  it("still bundles two real committed edges sharing a pair", () => {
    const edges = [edge("e1"), edge("e2")];
    const { renderEdges, bundleMembers } = groupEdgesForRender(edges);
    expect(renderEdges).toHaveLength(1);
    expect(bundleMembers.size).toBe(1);
  });
});

describe("groupEdgesForRender with network element attachments (ADR 0012 Attachments)", () => {
  // The pairKey is [source, target].sort().join("::"), keyed on node ids, so
  // it works unchanged for a device-to-element pair: no code change was
  // needed for this, only this test to pin the behavior.
  function attachEdge(id: string, sourcePort: string): Edge<LayerEdgeData> {
    return {
      id,
      source: "device-node",
      target: "element-node",
      data: { layer: "L2", source_port_name: sourcePort } as LayerEdgeData,
    };
  }

  it("bundles N device-to-element attachment edges into one BundledEdge with all N members", () => {
    const edges = [attachEdge("a1", "eth0"), attachEdge("a2", "eth1"), attachEdge("a3", "eth2")];
    const { renderEdges, bundleMembers } = groupEdgesForRender(edges);

    expect(renderEdges).toHaveLength(1);
    const bundle = renderEdges[0];
    expect(bundle.type).toBe("bundledEdge");
    expect(bundle.source).toBe("device-node");
    expect(bundle.target).toBe("element-node");
    expect(bundleMembers.get(bundle.id)).toEqual(["a1", "a2", "a3"]);
  });

  it("a single device-to-element attachment passes through unbundled", () => {
    const edges = [attachEdge("a1", "eth0")];
    const { renderEdges, bundleMembers } = groupEdgesForRender(edges);
    expect(renderEdges).toHaveLength(1);
    expect(renderEdges[0].id).toBe("a1");
    expect(bundleMembers.size).toBe(0);
  });
});
