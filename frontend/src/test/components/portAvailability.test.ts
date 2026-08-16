import { describe, it, expect } from "vitest";
import { computeCabledNames } from "@/components/topology-editor/wiring/portAvailability";
import { connectionFixture } from "../fixtures/wiringFixtures";

describe("computeCabledNames", () => {
  it("records both ports of a same-device loopback connection (review item 5)", () => {
    const deviceId = "d-1";
    // A real physical cable patching two ports on the SAME device to each
    // other: device_a_id === device_b_id === deviceId.
    const loopback = connectionFixture("c1", deviceId, "eth1", deviceId, "eth2");
    const cabled = computeCabledNames([loopback], deviceId);
    expect(cabled.has("eth1")).toBe(true);
    expect(cabled.has("eth2")).toBe(true);
  });

  it("records the one relevant port for a normal cross-device connection", () => {
    const deviceId = "d-1";
    const other = "d-2";
    const conn = connectionFixture("c1", deviceId, "eth1", other, "eth9");
    const cabled = computeCabledNames([conn], deviceId);
    expect(cabled.has("eth1")).toBe(true);
    expect(cabled.size).toBe(1);
  });

  it("ignores a connection that does not touch this device at all", () => {
    const conn = connectionFixture("c1", "d-2", "eth1", "d-3", "eth2");
    const cabled = computeCabledNames([conn], "d-1");
    expect(cabled.size).toBe(0);
  });
});
