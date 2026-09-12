import { describe, expect, it } from "vitest";
import type { Node } from "@xyflow/react";

import { routeProblemField, routeProblemFields, routeProblemLabel } from "@/lib/l3";
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

// ADR 0014 addendum X-I (issue #755): each validation reason maps to the route
// field it is ABOUT, so the Routing panel can outline the offending box.
describe("routeProblemField", () => {
  it("maps the two VRF-naming reasons to the virtual router field", () => {
    expect(routeProblemField("l3_unknown_virtual_router")).toBe("virtual_router");
    expect(routeProblemField("l3_interface_outside_virtual_router")).toBe("virtual_router");
  });

  it("maps l3_interface_bound_to_virtual_router to the interface field", () => {
    // This reason fires on a route that names NO VRF: its problem is that the
    // interface is enslaved to one, so the virtual router box is not at fault.
    expect(routeProblemField("l3_interface_bound_to_virtual_router")).toBe("interface");
  });

  it("keeps the pre-X-I reasons on their own fields", () => {
    expect(routeProblemField("l3_bad_destination")).toBe("destination");
    expect(routeProblemField("l3_bad_next_hop")).toBe("next_hop");
    expect(routeProblemField("l3_next_hop_unverifiable")).toBe("next_hop");
    expect(routeProblemField("l3_next_hop_outside_interface")).toBe("next_hop");
    expect(routeProblemField("l3_unknown_interface")).toBe("interface");
  });

  it("returns null for a switch-level reason and for anything unknown", () => {
    expect(routeProblemField("l3_switch_unattached")).toBeNull();
    expect(routeProblemField("l3_not_a_router")).toBeNull();
    expect(routeProblemField("l3_malformed")).toBeNull();
    expect(routeProblemField("something_new")).toBeNull();
  });
});

describe("routeProblemFields", () => {
  it("collects every blocking problem's field for one row", () => {
    const fields = routeProblemFields([
      { reason: "l3_unknown_virtual_router" },
      { reason: "l3_bad_destination" },
    ]);
    expect([...fields].sort()).toEqual(["destination", "virtual_router"]);
  });

  it("ignores the informational duplicate reason", () => {
    expect(routeProblemFields([{ reason: "l3_duplicate_route" }]).size).toBe(0);
  });

  it("ignores a reason that maps to no field", () => {
    expect(routeProblemFields([{ reason: "l3_switch_unattached" }]).size).toBe(0);
  });
});
