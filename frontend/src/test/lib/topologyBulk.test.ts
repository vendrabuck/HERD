import { describe, it, expect } from "vitest";
import {
  bulkDeleteConfirmLabel,
  bulkDeleteDescription,
  canDeleteTopologyAs,
  noneDeletableMessage,
  partitionTopologies,
  summarizeDelete,
} from "@/lib/topologyBulk";
import type { Topology } from "@/types/topology.types";

const OWNER_ID = "owner-id";

function topology(id: string, createdBy = OWNER_ID): Topology {
  return {
    id,
    name: `topo ${id}`,
    created_by: createdBy,
    owner_name: "owner",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

// cabling's delete_topology: allowed iff str(created_by) == sub, or role in
// ("admin", "superadmin"); anything else is a 403. Every caller shape the
// backend distinguishes, with the backend's answer.
const MATRIX: { caller: string; user: { id: string; role?: string | null } | null; ok: boolean }[] = [
  { caller: "owner (user role)", user: { id: OWNER_ID, role: "user" }, ok: true },
  { caller: "owner with no role", user: { id: OWNER_ID }, ok: true },
  { caller: "owner who is also admin", user: { id: OWNER_ID, role: "admin" }, ok: true },
  { caller: "admin, not owner", user: { id: "x", role: "admin" }, ok: true },
  { caller: "superadmin, not owner", user: { id: "x", role: "superadmin" }, ok: true },
  { caller: "other user", user: { id: "x", role: "user" }, ok: false },
  { caller: "other user with no role", user: { id: "x", role: null }, ok: false },
  // isAdminRole is case-sensitive, like the backend's tuple membership.
  { caller: "other user, role 'Admin'", user: { id: "x", role: "Admin" }, ok: false },
  { caller: "other user, unknown role", user: { id: "x", role: "operator" }, ok: false },
  { caller: "no user", user: null, ok: false },
];

describe("canDeleteTopologyAs (issue #958): the backend delete rule, exactly", () => {
  it.each(MATRIX)("$caller: $ok", ({ user, ok }) => {
    expect(canDeleteTopologyAs(topology("t"), user)).toBe(ok);
  });

  it("undefined user is refused", () => {
    expect(canDeleteTopologyAs(topology("t"), undefined)).toBe(false);
  });

  it("an empty user id never matches an empty created_by", () => {
    expect(canDeleteTopologyAs(topology("t", "other"), { id: "", role: "user" })).toBe(false);
  });
});

describe("partitionTopologies", () => {
  it("splits per row, never per batch, through the same predicate", () => {
    const mine = topology("1");
    const theirs = topology("2", "someone-else");
    const user = { id: OWNER_ID, role: "user" };
    expect(partitionTopologies([mine, theirs], user)).toEqual({
      eligible: [mine],
      notYours: [theirs],
    });
    for (const row of MATRIX) {
      const p = partitionTopologies([mine], row.user);
      expect(p.eligible.length === 1).toBe(row.ok);
    }
  });

  it("an admin's selection is all eligible", () => {
    const rows = [topology("1"), topology("2", "a"), topology("3", "b")];
    expect(partitionTopologies(rows, { id: "z", role: "admin" }).notYours).toEqual([]);
  });
});

describe("bulk delete wording", () => {
  const user = { id: OWNER_ID, role: "user" };

  it("all eligible: count, permanence, and a plural confirm label", () => {
    const p = partitionTopologies([topology("1"), topology("2")], user);
    expect(bulkDeleteDescription(p)).toBe(
      "Delete 2 topologies? This permanently deletes them and their canvas data and cannot be undone.",
    );
    expect(bulkDeleteConfirmLabel(p)).toBe("Delete 2 topologies");
  });

  it("singular label and wording", () => {
    const p = partitionTopologies([topology("1")], user);
    expect(bulkDeleteConfirmLabel(p)).toBe("Delete 1 topology");
    expect(bulkDeleteDescription(p)).toBe(
      "Delete 1 topology? This permanently deletes it and its canvas data and cannot be undone.",
    );
    const mixed = partitionTopologies([topology("1"), topology("2", "x")], user);
    expect(bulkDeleteDescription(mixed)).toBe(
      "Delete 1 of the 2 selected topologies? This permanently deletes it and its canvas " +
        "data and cannot be undone. 1 will be skipped: not yours.",
    );
  });

  it("some skipped: states the split and the reason", () => {
    const p = partitionTopologies(
      [topology("1"), topology("2", "x"), topology("3", "y"), topology("4")],
      user,
    );
    expect(bulkDeleteDescription(p)).toBe(
      "Delete 2 of the 4 selected topologies? This permanently deletes them and their " +
        "canvas data and cannot be undone. 2 will be skipped: not yours.",
    );
  });

  it("none eligible: the disabled reason", () => {
    const p = partitionTopologies([topology("1", "x")], user);
    expect(noneDeletableMessage(p)).toBe(
      "None of the selected topologies can be deleted: 1 not yours.",
    );
  });
});

describe("summarizeDelete", () => {
  const remaining = new Set<string>();
  it("all succeeded", () => {
    expect(summarizeDelete({ remaining, succeeded: 3, failures: [] })).toBe(
      "Deleted 3 topologies",
    );
    expect(summarizeDelete({ remaining, succeeded: 1, failures: [] })).toBe(
      "Deleted 1 topology",
    );
  });

  it("a shared reason is quoted", () => {
    expect(
      summarizeDelete({
        remaining,
        succeeded: 2,
        failures: [{ id: "a", reason: "Topology not found" }],
      }),
    ).toBe("Deleted 2, failed 1: Topology not found");
  });

  it("different or unknown reasons give counts only", () => {
    expect(
      summarizeDelete({
        remaining,
        succeeded: 0,
        failures: [
          { id: "a", reason: "one" },
          { id: "b", reason: "two" },
        ],
      }),
    ).toBe("Deleted 0, failed 2");
    expect(
      summarizeDelete({ remaining, succeeded: 1, failures: [{ id: "a", reason: null }] }),
    ).toBe("Deleted 1, failed 1");
  });
});
