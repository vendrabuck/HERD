import { describe, expect, it } from "vitest";
import type { Node } from "@xyflow/react";

import { routeProblemLabel } from "@/lib/l3";
import type { CanvasNodeData, InvalidRoute } from "@/types/topology.types";

// ADR 0014 phase 2 (issue #34) review fix F12: routeProblemLabel's fallback
// to the raw node_id when the node it names is no longer on the canvas was
// documented (l3.ts's own comment) but had no test. A stale result from a
// prior canvas (or, in principle, a node removed between the validate call
// resolving and this label being rendered) must degrade to the id, not
// throw.
describe("routeProblemLabel", () => {
  function invalidRoute(overrides: Partial<InvalidRoute> = {}): InvalidRoute {
    return {
      node_id: "missing-node",
      device_id: "d-1",
      index: 0,
      reason: "l3_bad_destination",
      detail: null,
      ...overrides,
    };
  }

  it("falls back to the raw node_id when the node is absent from the canvas", () => {
    const nodes: Node<CanvasNodeData>[] = [];
    expect(routeProblemLabel(invalidRoute(), nodes)).toBe("missing-node[0] l3_bad_destination");
  });

  it("renders a switch-level (index null) entry's index as '-'", () => {
    const nodes: Node<CanvasNodeData>[] = [];
    expect(routeProblemLabel(invalidRoute({ index: null, reason: "l3_switch_unattached" }), nodes)).toBe(
      "missing-node[-] l3_switch_unattached",
    );
  });
});
