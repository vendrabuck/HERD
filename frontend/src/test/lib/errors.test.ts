import {
  formatUnconnectableDetail,
  topologyUnconnectableDetail,
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
