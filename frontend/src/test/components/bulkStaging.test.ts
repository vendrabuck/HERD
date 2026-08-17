import { describe, it, expect } from "vitest";
import {
  applyBulkResult,
  buildBulkItems,
  existingPairKeys,
  pairKey,
  portKey,
  type StagedLine,
} from "@/components/admin/connections/bulkStaging";
import { connectionFixture } from "../fixtures/wiringFixtures";
import type { BulkConnectionResult } from "@/types/connection.types";

const DEVICE_A = "device-a";
const DEVICE_B = "device-b";

function line(id: string, sourceName: string, targetName: string): StagedLine {
  return {
    id,
    sourcePortId: `s-${id}`,
    sourcePortName: sourceName,
    targetPortId: `t-${id}`,
    targetPortName: targetName,
    duplicate: false,
    sourceCabled: false,
    targetCabled: false,
    error: null,
  };
}

describe("existingPairKeys", () => {
  it("keys a connection stored A to B in the staged (A port, B port) order", () => {
    const keys = existingPairKeys(
      [connectionFixture("c1", DEVICE_A, "eth1", DEVICE_B, "0/0/1")],
      DEVICE_A,
      DEVICE_B,
    );
    expect(keys.has(pairKey("eth1", "0/0/1"))).toBe(true);
    // Not the reversed orientation: the staged pair is oriented A to B.
    expect(keys.has(pairKey("0/0/1", "eth1"))).toBe(false);
  });

  it("normalizes a connection stored B to A back onto the staged orientation", () => {
    const keys = existingPairKeys(
      [connectionFixture("c1", DEVICE_B, "0/0/1", DEVICE_A, "eth1")],
      DEVICE_A,
      DEVICE_B,
    );
    expect(keys.has(pairKey("eth1", "0/0/1"))).toBe(true);
  });

  it("ignores connections that do not join the two selected devices", () => {
    const keys = existingPairKeys(
      [connectionFixture("c1", DEVICE_A, "eth1", "device-other", "x1")],
      DEVICE_A,
      DEVICE_B,
    );
    expect(keys.size).toBe(0);
  });

  it("records BOTH orientations of a loopback row when one device is on both sides", () => {
    // Independent checks, not else-if: with A === B a single row is a
    // duplicate candidate whichever way the admin stages it.
    const keys = existingPairKeys(
      [connectionFixture("c1", DEVICE_A, "eth1", DEVICE_A, "eth2")],
      DEVICE_A,
      DEVICE_A,
    );
    expect(keys.has(pairKey("eth1", "eth2"))).toBe(true);
    expect(keys.has(pairKey("eth2", "eth1"))).toBe(true);
  });
});

describe("portKey", () => {
  it("separates the same port id on two different devices", () => {
    expect(portKey("d1", "p1")).not.toBe(portKey("d2", "p1"));
  });

  it("collapses to one key when the same device is picked on both sides", () => {
    expect(portKey("d1", "p1")).toBe(portKey("d1", "p1"));
  });
});

describe("buildBulkItems", () => {
  it("stamps the batch type and notes onto every item", () => {
    const items = buildBulkItems(
      [line("1", "eth1", "0/0/1"), line("2", "eth2", "0/0/2")],
      DEVICE_A,
      DEVICE_B,
      "fiber",
      "rack 4 recable",
    );
    expect(items).toEqual([
      {
        device_a_id: DEVICE_A,
        port_a: "eth1",
        device_b_id: DEVICE_B,
        port_b: "0/0/1",
        connection_type: "fiber",
        notes: "rack 4 recable",
      },
      {
        device_a_id: DEVICE_A,
        port_a: "eth2",
        device_b_id: DEVICE_B,
        port_b: "0/0/2",
        connection_type: "fiber",
        notes: "rack 4 recable",
      },
    ]);
  });

  it("falls back to ethernet on a blank type and omits blank notes entirely", () => {
    const [item] = buildBulkItems([line("1", "eth1", "0/0/1")], DEVICE_A, DEVICE_B, "   ", "  ");
    expect(item.connection_type).toBe("ethernet");
    expect("notes" in item).toBe(false);
  });
});

describe("applyBulkResult", () => {
  const lines = [line("1", "eth1", "0/0/1"), line("2", "eth2", "0/0/2")];

  it("reports no summary and drops every line when all rows were created", () => {
    const result: BulkConnectionResult = {
      created: 2,
      rejected: 0,
      rows: [
        { index: 0, status: "created", connection_id: "c1", error: null },
        { index: 1, status: "created", connection_id: "c2", error: null },
      ],
    };
    expect(applyBulkResult(lines, result)).toEqual({ remaining: [], summary: null });
  });

  it("keeps only the rejected line, carrying the server reason", () => {
    const result: BulkConnectionResult = {
      created: 1,
      rejected: 1,
      rows: [
        { index: 0, status: "created", connection_id: "c1", error: null },
        { index: 1, status: "rejected", connection_id: null, error: "Port eth2 not found" },
      ],
    };
    const outcome = applyBulkResult(lines, result);
    expect(outcome.remaining).toHaveLength(1);
    expect(outcome.remaining[0].sourcePortName).toBe("eth2");
    expect(outcome.remaining[0].error).toBe("Port eth2 not found");
    expect(outcome.summary).toBe(
      "Created 1 of 2 connections, 1 rejected. Fix the flagged lines and retry.",
    );
  });

  it("keeps a line whose index has NO row: an absent row is not evidence of creation", () => {
    const result: BulkConnectionResult = {
      created: 1,
      rejected: 0,
      rows: [{ index: 0, status: "created", connection_id: "c1", error: null }],
    };
    const outcome = applyBulkResult(lines, result);
    expect(outcome.remaining).toHaveLength(1);
    expect(outcome.remaining[0].error).toBe("No result returned for this row");
  });

  it("names a reasonless rejection rather than rendering an empty error", () => {
    const result: BulkConnectionResult = {
      created: 0,
      rejected: 1,
      rows: [{ index: 0, status: "rejected", connection_id: null, error: null }],
    };
    const outcome = applyBulkResult([lines[0]], result);
    expect(outcome.remaining[0].error).toBe("Rejected without a reason");
    expect(outcome.summary).toBe(
      "Created 0 of 1 connection, 1 rejected. Fix the flagged lines and retry.",
    );
  });
});
