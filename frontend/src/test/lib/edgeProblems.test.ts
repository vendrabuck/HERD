import { describe, expect, it } from "vitest";
import type { Edge } from "@xyflow/react";

import {
  EMPTY_SERVER_EDGE_PROBLEMS,
  applySkippedConstrainedEdges,
  invalidEdgeReasonText,
  serverEdgeProblemsFrom,
  stripServerEdgeProblem,
  withServerEdgeProblems,
} from "@/lib/edgeProblems";
import type { LayerEdgeData } from "@/types/topology.types";

function edge(id: string, data?: Partial<LayerEdgeData>): Edge<LayerEdgeData> {
  return {
    id,
    source: "a",
    target: "b",
    type: "layerEdge",
    data: data === undefined ? undefined : ({ layer: "L1", ...data } as LayerEdgeData),
  } as Edge<LayerEdgeData>;
}

// Issue #1066: the fork canvas PUT's invalid_edges, as the editor's red state.
describe("serverEdgeProblemsFrom", () => {
  it("maps each reported edge id to its reason", () => {
    const problems = serverEdgeProblemsFrom([
      { edge_id: "e1", source_device_id: "d1", target_device_id: "d2", layer: "L1", reason: "no_port_path" },
      { edge_id: "e2", source_device_id: null, target_device_id: "d2", layer: null, reason: "missing_device" },
    ]);
    expect([...problems]).toEqual([
      ["e1", "no_port_path"],
      ["e2", "missing_device"],
    ]);
  });

  it("keeps the first reason when one edge id is reported twice", () => {
    const problems = serverEdgeProblemsFrom([
      { edge_id: "e1", reason: "no_path" },
      { edge_id: "e1", reason: "no_port_path" },
    ]);
    expect(problems.get("e1")).toBe("no_path");
  });

  it("skips entries it cannot place on a line", () => {
    const problems = serverEdgeProblemsFrom([
      null,
      "e1",
      { reason: "no_path" },
      { edge_id: "", reason: "no_path" },
      { edge_id: 7, reason: "no_path" },
      { edge_id: "e2" },
      { edge_id: "e3", reason: "" },
      { edge_id: "e4", reason: "no_path" },
    ]);
    expect([...problems]).toEqual([["e4", "no_path"]]);
  });

  it("answers the shared empty map for an empty list or a value that is not a list", () => {
    for (const value of [[], undefined, null, {}, "no_path"]) {
      expect(serverEdgeProblemsFrom(value)).toBe(EMPTY_SERVER_EDGE_PROBLEMS);
    }
  });
});

describe("applySkippedConstrainedEdges", () => {
  it("replaces every no_port_path verdict with the save's skipped list and keeps other reasons", () => {
    const current = new Map([
      ["e-fixed", "no_port_path"],
      ["e-element", "element_edge_no_port"],
    ]);
    const next = applySkippedConstrainedEdges(current, [
      { edge_id: "e-new" },
      { edge_id: null },
      { edge_id: "" },
    ]);
    expect([...next]).toEqual([
      ["e-element", "element_edge_no_port"],
      ["e-new", "no_port_path"],
    ]);
    // The input map is left as it was.
    expect(current.get("e-fixed")).toBe("no_port_path");
  });

  it("clears the no_port_path verdicts when the save skipped nothing, or omitted the field", () => {
    const current = new Map([["e1", "no_port_path"]]);
    expect(applySkippedConstrainedEdges(current, [])).toBe(EMPTY_SERVER_EDGE_PROBLEMS);
    expect(applySkippedConstrainedEdges(current, undefined)).toBe(EMPTY_SERVER_EDGE_PROBLEMS);
  });
});

describe("withServerEdgeProblems", () => {
  it("returns the same array when there is nothing to overlay", () => {
    const edges = [edge("e1"), edge("e2")];
    expect(withServerEdgeProblems(edges, EMPTY_SERVER_EDGE_PROBLEMS)).toBe(edges);
    expect(withServerEdgeProblems(edges, new Map([["gone", "no_path"]]))).toBe(edges);
  });

  it("overlays the reason on copies of the matched edges only, never mutating the input", () => {
    const e1 = edge("e1", { pathValid: true });
    const e2 = edge("e2");
    const result = withServerEdgeProblems([e1, e2], new Map([["e1", "no_port_path"]]));
    expect(result[0]).not.toBe(e1);
    expect(result[0].data).toEqual({ layer: "L1", pathValid: true, serverInvalidReason: "no_port_path" });
    expect(result[1]).toBe(e2);
    expect(e1.data).toEqual({ layer: "L1", pathValid: true });
  });

  it("gives a data-less edge a default layer alongside the reason", () => {
    const result = withServerEdgeProblems([edge("e1")], new Map([["e1", "no_path"]]));
    expect(result[0].data).toEqual({ layer: "L2", serverInvalidReason: "no_path" });
  });
});

describe("stripServerEdgeProblem", () => {
  it("removes the render-only reason and keeps everything else", () => {
    const stripped = stripServerEdgeProblem(
      edge("e1", { source_port_name: "p1", serverInvalidReason: "no_port_path" }),
    );
    expect(stripped.data).toEqual({ layer: "L1", source_port_name: "p1" });
  });

  it("returns the same edge when there is nothing to strip", () => {
    const plain = edge("e1", { pathValid: false });
    expect(stripServerEdgeProblem(plain)).toBe(plain);
    const dataless = edge("e2");
    expect(stripServerEdgeProblem(dataless)).toBe(dataless);
  });
});

describe("invalidEdgeReasonText", () => {
  it("words every reason cabling's validator uses and passes an unknown one through", () => {
    expect(invalidEdgeReasonText("no_path")).toBe("no cable path");
    expect(invalidEdgeReasonText("no_port_path")).toBe("no cable path on the chosen ports");
    expect(invalidEdgeReasonText("missing_device")).toBe("device not found");
    expect(invalidEdgeReasonText("element_to_element")).toBe("cannot connect two elements directly");
    expect(invalidEdgeReasonText("element_edge_no_port")).toBe("element has no available port");
    expect(invalidEdgeReasonText("brand_new")).toBe("brand_new");
  });
});
