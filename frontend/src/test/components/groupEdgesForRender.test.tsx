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
