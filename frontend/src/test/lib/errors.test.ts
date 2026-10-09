import {
  configApplyErrorText,
  configRestoreErrorText,
  deletePortErrorText,
  driverCannotConfigureDetail,
  formatRestoreBlocked,
  restoreBlockedDetail,
  formatMixedTypesDetail,
  formatTopologyInUse,
  formatUnconnectableDetail,
  LOGIN_LOCKED_FALLBACK,
  loginRetryAfterSeconds,
  loginRetryAfterText,
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

describe("deletePortErrorText (issue #1023)", () => {
  it("names the connection count for a port_cabled refusal", () => {
    expect(
      deletePortErrorText(
        axiosLike(409, { error: "port_cabled", connection_count: 1, connection_ids: ["c1"] }),
      ),
    ).toBe("Port is still cabled (1 connection) and cannot be deleted. Remove its cables first.");
    expect(
      deletePortErrorText(
        axiosLike(409, { error: "port_cabled", connection_count: 12, connection_ids: [] }),
      ),
    ).toBe("Port is still cabled (12 connections) and cannot be deleted. Remove its cables first.");
  });

  it("drops the count when it is missing or not a positive number", () => {
    for (const count of [undefined, 0, -1, "3"]) {
      expect(
        deletePortErrorText(axiosLike(409, { error: "port_cabled", connection_count: count })),
      ).toBe("Port is still cabled and cannot be deleted. Remove its cables first.");
    }
  });

  it("passes a plain-string detail through (404, the fail-closed 503)", () => {
    expect(deletePortErrorText(axiosLike(503, "Could not verify port is not cabled"))).toBe(
      "Could not verify port is not cabled",
    );
    expect(deletePortErrorText(axiosLike(404, "Port not found"))).toBe("Port not found");
  });

  it("falls back for any other shape, never returning an object", () => {
    expect(deletePortErrorText(axiosLike(409, { error: "device_cabled" }))).toBe(
      "Failed to delete port",
    );
    expect(deletePortErrorText(axiosLike(500, { error: "port_cabled" }))).toBe(
      "Failed to delete port",
    );
    expect(deletePortErrorText(new Error("network"))).toBe("Failed to delete port");
  });
});

const GATE_MESSAGE =
  "This device's driver implements the Layer 3 Switch contract, which has no configure " +
  "method, so a config apply cannot run. Config versions on this device store intent only.";

describe("configApplyErrorText (issue #1098)", () => {
  it("shows the driver gate's sentence and the driver's name for the structured 409", () => {
    const err = axiosLike(409, {
      error: "driver_cannot_configure",
      connection_type: "Layer 3 Switch",
      driver: "frr_l3",
      message: GATE_MESSAGE,
    });
    expect(driverCannotConfigureDetail(err)).not.toBeNull();
    expect(configApplyErrorText(err, "Apply request failed")).toBe(
      GATE_MESSAGE + " (driver: frr_l3)",
    );
  });

  it("builds a sentence from the connection type when the message is missing", () => {
    expect(
      configApplyErrorText(
        axiosLike(409, { error: "driver_cannot_configure", connection_type: "Layer 2 Switch" }),
        "Apply request failed",
      ),
    ).toBe(
      "This device's driver implements the Layer 2 Switch contract, which has no configure " +
        "method, so a config apply cannot run.",
    );
    expect(
      configApplyErrorText(axiosLike(409, { error: "driver_cannot_configure" }), "x"),
    ).toBe("This device's driver has no configure method, so a config apply cannot run.");
  });

  it("passes a plain-string detail through (403, 422, the fail-closed 503)", () => {
    const forbidden =
      "manage grant required on this device for an immediate apply " +
      "(a reservation owner can schedule the apply instead)";
    expect(configApplyErrorText(axiosLike(403, forbidden), "Apply request failed")).toBe(
      forbidden,
    );
    expect(
      configApplyErrorText(axiosLike(503, "reservations service unreachable"), "Schedule failed"),
    ).toBe("reservations service unreachable");
  });

  it("falls back for any other shape, never returning an object", () => {
    expect(configApplyErrorText(axiosLike(409, { error: "other" }), "Schedule failed")).toBe(
      "Schedule failed",
    );
    expect(
      configApplyErrorText(axiosLike(500, { error: "driver_cannot_configure" }), "Apply failed"),
    ).toBe("Apply failed");
    expect(configApplyErrorText(axiosLike(422, [{ msg: "bad" }]), "Schedule failed")).toBe(
      "Schedule failed",
    );
    expect(configApplyErrorText(new Error("network"), "Apply request failed")).toBe(
      "Apply request failed",
    );
  });
});

describe("configRestoreErrorText (issue #1098)", () => {
  const RES = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: `${i}${i}${i}${i}aaaa-0000-0000-0000-000000000000`,
      status: "ACTIVE",
      end_time: "2026-10-09T00:00:00Z",
    }));

  it("lists the blocking reservations by short id and status", () => {
    const err = axiosLike(409, {
      message: "Device has active reservations; restore blocked",
      reservations: RES(2),
    });
    expect(restoreBlockedDetail(err)).not.toBeNull();
    expect(configRestoreErrorText(err)).toBe(
      "Device has active reservations; restore blocked: 0000aaaa (ACTIVE), 1111aaaa (ACTIVE)",
    );
  });

  it("shows at most three ids, then the count of the rest", () => {
    expect(
      formatRestoreBlocked({
        message: "Device has active reservations; restore blocked",
        reservations: RES(5),
      }),
    ).toBe(
      "Device has active reservations; restore blocked: 0000aaaa (ACTIVE), 1111aaaa (ACTIVE), " +
        "2222aaaa (ACTIVE) and 2 more",
    );
  });

  it("keeps the sentence when the message or the ids are missing", () => {
    expect(formatRestoreBlocked({ reservations: [] })).toBe(
      "Device has active reservations; restore blocked",
    );
    expect(formatRestoreBlocked({ reservations: [{ id: "abcdef0123", status: null }] })).toBe(
      "Device has active reservations; restore blocked: abcdef01",
    );
  });

  it("passes a plain-string detail through (403, 422, the fail-closed 503)", () => {
    const unreachable = "reservations service unreachable while checking active reservations";
    expect(configRestoreErrorText(axiosLike(503, unreachable))).toBe(unreachable);
    expect(configRestoreErrorText(axiosLike(403, "manage permission required"))).toBe(
      "manage permission required",
    );
  });

  it("falls back for any other shape, never returning an object", () => {
    expect(configRestoreErrorText(axiosLike(409, { message: "x" }))).toBe("Restore failed");
    expect(configRestoreErrorText(axiosLike(503, { reservations: [] }))).toBe("Restore failed");
    expect(configRestoreErrorText(new Error("network"))).toBe("Restore failed");
  });
});

// Issue #1126: the config login's 429 from the attempt limit (OPS-CONFIG-22).
describe("loginRetryAfterSeconds and loginRetryAfterText", () => {
  function locked(headers: unknown, detail: unknown = LOGIN_LOCKED_FALLBACK, status = 429) {
    return { response: { status, headers, data: { detail } } };
  }

  it("reads a whole-second Retry-After from a plain header object, in any case", () => {
    expect(loginRetryAfterSeconds(locked({ "retry-after": "12" }))).toBe(12);
    expect(loginRetryAfterSeconds(locked({ "Retry-After": " 7 " }))).toBe(7);
    expect(loginRetryAfterText(locked({ "retry-after": "12" }))).toBe(
      "Too many failed login attempts; try again in 12 seconds",
    );
    expect(loginRetryAfterText(locked({ "retry-after": "1" }))).toBe(
      "Too many failed login attempts; try again in 1 second",
    );
  });

  it("reads Retry-After through an AxiosHeaders-style get()", () => {
    const headers = {
      get(name: string) {
        return name.toLowerCase() === "retry-after" ? "45" : undefined;
      },
    };
    expect(loginRetryAfterSeconds(locked(headers))).toBe(45);
  });

  it("falls back to the server's sentence when Retry-After is missing or unusable", () => {
    for (const headers of [
      {},
      undefined,
      { "retry-after": "" },
      { "retry-after": "0" },
      { "retry-after": "-3" },
      { "retry-after": "2.5" },
      { "retry-after": "Wed, 21 Oct 2026 07:28:00 GMT" },
    ]) {
      expect(loginRetryAfterSeconds(locked(headers))).toBeNull();
      expect(loginRetryAfterText(locked(headers))).toBe(LOGIN_LOCKED_FALLBACK);
    }
    expect(loginRetryAfterText(locked({}, "Slow down"))).toBe("Slow down");
  });

  it("writes the sentence itself when the 429 detail is not a string", () => {
    expect(loginRetryAfterText(locked({}, [{ msg: "x" }]))).toBe(
      "Too many failed login attempts; try again later",
    );
  });

  it("answers null for anything that is not a 429", () => {
    for (const err of [
      locked({ "retry-after": "10" }, "Invalid password", 401),
      locked({ "retry-after": "10" }, "boom", 503),
      new Error("Network Error"),
      undefined,
    ]) {
      expect(loginRetryAfterSeconds(err)).toBeNull();
      expect(loginRetryAfterText(err)).toBeNull();
    }
  });
});
