import { describe, expect, it } from "vitest";
import { deleteDeviceErrorMessage, deviceCabledCount } from "@/api/inventory";

function err(detail: unknown) {
  return { response: { data: { detail } } };
}

const RID = "22222222-2222-2222-2222-222222222222";

describe("deleteDeviceErrorMessage", () => {
  it("passes a plain string detail through", () => {
    expect(deleteDeviceErrorMessage(err("Device not found"))).toBe("Device not found");
  });

  it("keeps the member wording when there is no transit holder", () => {
    expect(
      deleteDeviceErrorMessage(
        err({ error: "device_in_use", reservation_ids: ["r1"], transit_reservation_ids: [] }),
      ),
    ).toBe("Device is held by an active reservation and cannot be deleted");
  });

  it("keeps the member wording for a pre-#900 detail with no transit key", () => {
    expect(
      deleteDeviceErrorMessage(err({ error: "device_in_use", reservation_ids: ["r1"] })),
    ).toBe("Device is held by an active reservation and cannot be deleted");
  });

  it("names a single transit-only reservation", () => {
    expect(
      deleteDeviceErrorMessage(
        err({ error: "device_in_use", reservation_ids: [RID], transit_reservation_ids: [RID] }),
      ),
    ).toBe("Device carries live wiring for reservation 22222222 as a transit hop and cannot be deleted");
  });

  it("counts several transit-only reservations", () => {
    expect(
      deleteDeviceErrorMessage(
        err({
          error: "device_in_use",
          reservation_ids: ["a", "b"],
          transit_reservation_ids: ["a", "b"],
        }),
      ),
    ).toBe("Device carries live wiring for 2 reservations as a transit hop and cannot be deleted");
  });

  it("mentions both causes when the device is a member and a transit hop", () => {
    expect(
      deleteDeviceErrorMessage(
        err({ error: "device_in_use", reservation_ids: ["m", RID], transit_reservation_ids: [RID] }),
      ),
    ).toBe(
      "Device is held by an active reservation and carries live wiring for reservation 22222222 as a transit hop, so it cannot be deleted",
    );
  });

  it("falls back to the generic message for an unknown shape", () => {
    expect(deleteDeviceErrorMessage(err({ error: "other" }))).toBe("Failed to delete device");
    expect(deleteDeviceErrorMessage(new Error("x"))).toBe("Failed to delete device");
  });

  it("tells the admin to remove the cables first, with the connection count (issue #940)", () => {
    expect(
      deleteDeviceErrorMessage(
        err({ error: "device_cabled", connection_count: 3, connection_ids: ["a", "b", "c"] }),
      ),
    ).toBe("Device is still cabled (3 connections) and cannot be deleted. Remove its cables first.");
  });

  it("uses the singular for one connection", () => {
    expect(
      deleteDeviceErrorMessage(
        err({ error: "device_cabled", connection_count: 1, connection_ids: ["a"] }),
      ),
    ).toBe("Device is still cabled (1 connection) and cannot be deleted. Remove its cables first.");
  });

  it("reports the true total, not the capped id sample", () => {
    expect(
      deleteDeviceErrorMessage(
        err({ error: "device_cabled", connection_count: 57, connection_ids: ["a", "b"] }),
      ),
    ).toContain("57 connections");
  });

  it("still gives a cabled message when the count is missing", () => {
    expect(deleteDeviceErrorMessage(err({ error: "device_cabled" }))).toBe(
      "Device is still cabled and cannot be deleted. Remove its cables first.",
    );
  });
});

describe("deviceCabledCount", () => {
  it("returns the count for device_cabled", () => {
    expect(deviceCabledCount(err({ error: "device_cabled", connection_count: 2 }))).toBe(2);
  });

  it("returns null for any other refusal or shape", () => {
    expect(deviceCabledCount(err({ error: "device_in_use", reservation_ids: [] }))).toBeNull();
    expect(deviceCabledCount(err("Device not found"))).toBeNull();
    expect(deviceCabledCount(new Error("network"))).toBeNull();
    expect(deviceCabledCount(undefined)).toBeNull();
  });
});
