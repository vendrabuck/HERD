import {
  formatMixedTypesDetail,
  formatTopologyInUse,
  formatUnconnectableDetail,
  topologyMixedTypesDetail,
  topologyDeleteErrorText,
  topologyInUseDetail,
  topologyUnconnectableDetail,
  type TopologyMixedTypesDetail,
  type TopologyUnconnectableDetail,
} from "@/lib/errors";

function axiosLike(status: number, detail: unknown) {
  return { response: { status, data: { detail } } };
}

const DETAIL: TopologyUnconnectableDetail = {
  error: "topology_unconnectable",
  pairs: [
    {
      source_role: "fw-a",
      target_role: "client",
      source_template: "EX3400",
      target_template: "Ubuntu Client",
    },
    {
      source_role: "fw-b",
      target_role: "client",
      source_template: "EX3400",
      target_template: "Ubuntu Client",
    },
  ],
  message: "The lab has no cabled path for 2 proposed connections.",
};

describe("topologyUnconnectableDetail", () => {
  it("narrows the structured 422 body", () => {
    expect(topologyUnconnectableDetail(axiosLike(422, DETAIL))).toEqual(DETAIL);
  });

  it("returns null for a different status carrying the same body", () => {
    expect(topologyUnconnectableDetail(axiosLike(409, DETAIL))).toBeNull();
  });

  it("returns null for a plain-string detail", () => {
    expect(topologyUnconnectableDetail(axiosLike(422, "Unprocessable"))).toBeNull();
  });

  it("returns null for another structured 422 (the fork malformed shape)", () => {
    expect(
      topologyUnconnectableDetail(axiosLike(422, { error: "l3_intent_malformed" })),
    ).toBeNull();
  });

  it("returns null when pairs is not an array", () => {
    expect(
      topologyUnconnectableDetail(
        axiosLike(422, { error: "topology_unconnectable", pairs: null, message: "x" }),
      ),
    ).toBeNull();
  });

  it("returns null for a non-axios error", () => {
    expect(topologyUnconnectableDetail(new Error("boom"))).toBeNull();
  });
});

describe("formatUnconnectableDetail", () => {
  it("renders one 'source to target' line per pair under the message", () => {
    expect(formatUnconnectableDetail(DETAIL)).toBe(
      "The lab has no cabled path for 2 proposed connections.\nfw-a to client\nfw-b to client",
    );
  });

  it("falls back to a sentence when the server sends none", () => {
    const text = formatUnconnectableDetail({ ...DETAIL, message: "" });
    expect(text).toMatch(/cannot be wired/);
    expect(text).toContain("fw-a to client");
  });

  it("returns the message alone when there are no pairs", () => {
    expect(formatUnconnectableDetail({ ...DETAIL, pairs: [] })).toBe(DETAIL.message);
  });
});

describe("topologyMixedTypesDetail (#1038)", () => {
  const MIXED: TopologyMixedTypesDetail = {
    error: "topology_mixed_types",
    groups: [
      { topology_type: "CLOUD", roles: ["vm"], templates: ["CloudVM"] },
      { topology_type: "PHYSICAL", roles: ["fw", "sw"], templates: ["EX3400", "QFX"] },
    ],
    message: "The proposal mixes CLOUD and PHYSICAL devices.",
  };

  it("narrows the structured 422 body", () => {
    expect(topologyMixedTypesDetail(axiosLike(422, MIXED))).toEqual(MIXED);
  });

  it("returns null for the unconnectable 422 and a plain string", () => {
    expect(topologyMixedTypesDetail(axiosLike(422, DETAIL))).toBeNull();
    expect(topologyMixedTypesDetail(axiosLike(422, "Unprocessable"))).toBeNull();
    expect(topologyMixedTypesDetail(axiosLike(409, MIXED))).toBeNull();
  });

  it("renders the message then one line per type", () => {
    expect(formatMixedTypesDetail(MIXED)).toBe(
      "The proposal mixes CLOUD and PHYSICAL devices.\nCLOUD: CloudVM\nPHYSICAL: EX3400, QFX",
    );
  });
});

describe("topologyInUseDetail (#977)", () => {
  const IN_USE = { error: "topology_in_use", reservation_ids: ["r-1", "r-2"] };

  it("narrows the structured 409 body", () => {
    expect(topologyInUseDetail(axiosLike(409, IN_USE))).toEqual(IN_USE);
  });

  it("narrows an empty id list (the shape still matches)", () => {
    const empty = { error: "topology_in_use", reservation_ids: [] };
    expect(topologyInUseDetail(axiosLike(409, empty))).toEqual(empty);
  });

  it.each([
    ["a different status", axiosLike(503, IN_USE)],
    ["a plain-string 409", axiosLike(409, "Conflict")],
    ["another structured 409", axiosLike(409, { error: "device_in_use", reservation_ids: [] })],
    ["reservation_ids missing", axiosLike(409, { error: "topology_in_use" })],
    ["reservation_ids not a list", axiosLike(409, { error: "topology_in_use", reservation_ids: "r" })],
    ["a non-string id", axiosLike(409, { error: "topology_in_use", reservation_ids: [1] })],
    ["a non-axios error", new Error("boom")],
  ])("returns null for %s", (_label, err) => {
    expect(topologyInUseDetail(err)).toBeNull();
  });
});

describe("formatTopologyInUse (#977)", () => {
  it("uses the singular for one reservation", () => {
    expect(formatTopologyInUse({ error: "topology_in_use", reservation_ids: ["r-1"] })).toBe(
      "In use by 1 reservation. Cancel it or wait for it to end, then delete again.",
    );
  });

  it("uses the plural for several", () => {
    expect(
      formatTopologyInUse({ error: "topology_in_use", reservation_ids: ["r-1", "r-2", "r-3"] }),
    ).toBe("In use by 3 reservations. Cancel them or wait for them to end, then delete again.");
  });
});

describe("topologyDeleteErrorText (#977)", () => {
  it("gives the in-use wording for the structured 409", () => {
    expect(
      topologyDeleteErrorText(
        axiosLike(409, { error: "topology_in_use", reservation_ids: ["r-1"] }),
        "fallback",
      ),
    ).toBe("In use by 1 reservation. Cancel it or wait for it to end, then delete again.");
  });

  it("passes a plain-string detail through (the fail-closed 503)", () => {
    expect(
      topologyDeleteErrorText(axiosLike(503, "Could not verify topology is not in use"), "fb"),
    ).toBe("Could not verify topology is not in use");
  });

  it("falls back when the 409 detail is not in shape", () => {
    expect(
      topologyDeleteErrorText(axiosLike(409, { error: "topology_in_use" }), "fallback"),
    ).toBe("fallback");
  });
});
