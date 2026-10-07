import { describe, it, expect } from "vitest";
import { deviceEditPayload, type DeviceEditFields } from "@/lib/deviceEdit";

const BASE: DeviceEditFields = {
  name: "core-fw-01",
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
  field_data: { mgmt_ip: "10.0.0.1", enabled: true },
};

describe("deviceEditPayload (issue #1020)", () => {
  it("is empty when nothing changed", () => {
    expect(deviceEditPayload(BASE, { ...BASE })).toEqual({});
  });

  it("sends only the changed name, trimmed, and never the status", () => {
    expect(deviceEditPayload(BASE, { ...BASE, name: "  renamed  " })).toEqual({ name: "renamed" });
  });

  it("treats a name that differs only in surrounding space as unchanged", () => {
    expect(deviceEditPayload(BASE, { ...BASE, name: " core-fw-01 " })).toEqual({});
  });

  it("sends status and topology only when the admin changed them", () => {
    expect(
      deviceEditPayload(BASE, { ...BASE, status: "OFFLINE", topology_type: "CLOUD" }),
    ).toEqual({ status: "OFFLINE", topology_type: "CLOUD" });
  });

  it("sends the whole field_data object when any key in it changed", () => {
    const field_data = { ...BASE.field_data, mgmt_ip: "10.0.0.2" };
    expect(deviceEditPayload(BASE, { ...BASE, field_data })).toEqual({ field_data });
  });

  it("sends field_data when a key was removed", () => {
    expect(deviceEditPayload(BASE, { ...BASE, field_data: { enabled: true } })).toEqual({
      field_data: { enabled: true },
    });
  });
});
