import {
  applySettled,
  confirmDescription,
  confirmLabel,
  describeSkipped,
  noneEligibleMessage,
  partitionSelection,
  sharedReason,
  summarizeOutcome,
} from "@/lib/reservationBulk";
import type { BulkAction, SkipReason } from "@/lib/reservationBulk";
import { canCancelAs, canReleaseAs } from "@/lib/reservationStatus";
import type { Reservation, ReservationStatus } from "@/types/reservation.types";

const ME = "me";
const OTHER = "someone-else";
const OWNER = { id: ME, role: "user" };
const ADMIN = { id: "admin-id", role: "admin" };
const STRANGER = { id: "stranger", role: "user" };

function res(id: string, status: ReservationStatus, userId = ME): Reservation {
  return { id, user_id: userId, status } as Reservation;
}

const STATUSES: ReservationStatus[] = [
  "PENDING",
  "PENDING_PROVISION",
  "ACTIVE",
  "COMPLETED",
  "CANCELLED",
  "FAILED",
];

// Reservations here are owned by ME. Three caller kinds by every status: the
// owner, an admin who does not own it, and a non-admin who does not own it.
// Cancel follows the backend rule (owner or admin), Release is owner only.
const NOT_YOURS_FOR_CALLER: Record<string, { cancel: boolean; release: boolean }> = {
  owner: { cancel: false, release: false },
  admin: { cancel: false, release: true },
  stranger: { cancel: true, release: true },
};
const CALLERS = { owner: OWNER, admin: ADMIN, stranger: STRANGER };

const EXPECT: Record<BulkAction, Record<ReservationStatus, SkipReason | null>> = {
  cancel: {
    PENDING: null,
    PENDING_PROVISION: null,
    ACTIVE: null,
    COMPLETED: "finished",
    CANCELLED: "finished",
    FAILED: "finished",
  },
  release: {
    PENDING: "not_active",
    PENDING_PROVISION: "not_active",
    ACTIVE: null,
    COMPLETED: "finished",
    CANCELLED: "finished",
    FAILED: "finished",
  },
};

describe("partitionSelection eligibility matrix", () => {
  for (const action of ["cancel", "release"] as const) {
    for (const [kind, caller] of Object.entries(CALLERS)) {
      it.each(STATUSES)(`${action}: ${kind}, status %s`, (status) => {
        const { eligible, skipped } = partitionSelection(action, [res("a", status)], caller);
        const notYours = NOT_YOURS_FOR_CALLER[kind][action];
        const want = notYours ? "not_yours" : EXPECT[action][status];
        if (want === null) {
          expect(eligible.map((r) => r.id)).toEqual(["a"]);
          expect(skipped).toEqual([]);
        } else {
          expect(eligible).toEqual([]);
          expect(skipped.map((s) => s.reason)).toEqual([want]);
        }
      });
    }

    it(`${action}: no signed-in user means nothing is eligible`, () => {
      const { eligible, skipped } = partitionSelection(action, [res("a", "ACTIVE")], undefined);
      expect(eligible).toEqual([]);
      expect(skipped[0].reason).toBe("not_yours");
    });
  }

  it("agrees with the single-row gates for every status, owner and caller kind", () => {
    for (const status of STATUSES) {
      for (const owner of [ME, OTHER]) {
        for (const caller of Object.values(CALLERS)) {
          const r = res("a", status, owner);
          expect(partitionSelection("cancel", [r], caller).eligible.length === 1).toBe(
            canCancelAs(r, caller),
          );
          expect(partitionSelection("release", [r], caller).eligible.length === 1).toBe(
            canReleaseAs(r, caller),
          );
        }
      }
    }
  });

  it("partitions a mixed selection per row, keeping order", () => {
    const rows = [res("1", "ACTIVE"), res("2", "CANCELLED"), res("3", "PENDING"), res("4", "ACTIVE", OTHER)];
    const cancel = partitionSelection("cancel", rows, OWNER);
    expect(cancel.eligible.map((r) => r.id)).toEqual(["1", "3"]);
    expect(cancel.skipped.map((s) => [s.reservation.id, s.reason])).toEqual([
      ["2", "finished"],
      ["4", "not_yours"],
    ]);
    const release = partitionSelection("release", rows, OWNER);
    expect(release.eligible.map((r) => r.id)).toEqual(["1"]);
    expect(release.skipped.map((s) => s.reason)).toEqual(["finished", "not_active", "not_yours"]);
  });

  it("lets an admin cancel across owners but release only their own", () => {
    const rows = [res("1", "ACTIVE", OTHER), res("2", "ACTIVE", ADMIN.id)];
    expect(partitionSelection("cancel", rows, ADMIN).eligible.map((r) => r.id)).toEqual(["1", "2"]);
    const release = partitionSelection("release", rows, ADMIN);
    expect(release.eligible.map((r) => r.id)).toEqual(["2"]);
    expect(release.skipped.map((s) => s.reason)).toEqual(["not_yours"]);
  });
});

describe("applySettled", () => {
  const ok = { status: "fulfilled", value: undefined } as const;
  const fail = (reason: unknown) => ({ status: "rejected", reason }) as const;
  const reasonOf = (e: unknown) => (typeof e === "string" ? e : null);

  it("drops every id when all succeed", () => {
    const out = applySettled(new Set(["a", "b", "c"]), ["a", "b"], [ok, ok], reasonOf);
    expect([...out.remaining]).toEqual(["c"]);
    expect(out.succeeded).toBe(2);
    expect(out.failures).toEqual([]);
  });

  it("keeps failed ids selected with the server's reason", () => {
    const out = applySettled(new Set(["a", "b", "c"]), ["a", "b", "c"], [ok, fail("busy"), ok], reasonOf);
    expect([...out.remaining]).toEqual(["b"]);
    expect(out.succeeded).toBe(2);
    expect(out.failures).toEqual([{ id: "b", reason: "busy" }]);
  });

  it("keeps an id with no result and counts it as a failure", () => {
    const out = applySettled(new Set(["a", "b"]), ["a", "b"], [ok], reasonOf);
    expect([...out.remaining]).toEqual(["b"]);
    expect(out.succeeded).toBe(1);
    expect(out.failures).toEqual([{ id: "b", reason: null }]);
  });

  it("does not mutate the input selection", () => {
    const selection = new Set(["a"]);
    applySettled(selection, ["a"], [ok], reasonOf);
    expect([...selection]).toEqual(["a"]);
  });
});

describe("summaries", () => {
  const fails = (...reasons: (string | null)[]) => reasons.map((reason, i) => ({ id: `f${i}`, reason }));

  it("sharedReason is the common reason only when every failure agrees", () => {
    expect(sharedReason([])).toBeNull();
    expect(sharedReason(fails("x", "x"))).toBe("x");
    expect(sharedReason(fails("x", "y"))).toBeNull();
    expect(sharedReason(fails("x", null))).toBeNull();
    expect(sharedReason(fails(null))).toBeNull();
    expect(sharedReason(fails(""))).toBeNull();
  });

  it("reports all-success with singular and plural nouns", () => {
    const base = { remaining: new Set<string>(), failures: [] };
    expect(summarizeOutcome("cancel", { ...base, succeeded: 1 })).toBe("Cancelled 1 reservation");
    expect(summarizeOutcome("cancel", { ...base, succeeded: 3 })).toBe("Cancelled 3 reservations");
    expect(summarizeOutcome("release", { ...base, succeeded: 2 })).toBe("Released 2 reservations");
  });

  it("appends the reason when all failures share one", () => {
    const out = { remaining: new Set<string>(), succeeded: 2, failures: fails("Reservation not found", "Reservation not found") };
    expect(summarizeOutcome("cancel", out)).toBe("Cancelled 2, failed 2: Reservation not found");
  });

  it("gives only the counts when reasons differ or are unknown", () => {
    const mixed = { remaining: new Set<string>(), succeeded: 1, failures: fails("a", "b") };
    expect(summarizeOutcome("release", mixed)).toBe("Released 1, failed 2");
    const unknown = { remaining: new Set<string>(), succeeded: 0, failures: fails(null) };
    expect(summarizeOutcome("cancel", unknown)).toBe("Cancelled 0, failed 1");
  });
});

describe("confirmation text", () => {
  it("states the consequence with no skipped rows (singular and plural)", () => {
    const one = partitionSelection("cancel", [res("1", "ACTIVE")], OWNER);
    expect(confirmDescription("cancel", one)).toBe(
      "Cancel 1 reservation? This releases their devices and cannot be undone.",
    );
    expect(confirmLabel("cancel", one)).toBe("Cancel 1 reservation");
    const two = partitionSelection("release", [res("1", "ACTIVE"), res("2", "ACTIVE")], OWNER);
    expect(confirmDescription("release", two)).toBe(
      "Release 2 reservations? This ends them early and frees their devices.",
    );
  });

  it("reports how many will be acted on and how many skipped, with reasons", () => {
    const rows = [res("1", "ACTIVE"), res("2", "CANCELLED"), res("3", "COMPLETED"), res("4", "ACTIVE", OTHER)];
    const p = partitionSelection("cancel", rows, OWNER);
    expect(confirmDescription("cancel", p)).toBe(
      "Cancel 1 of the 4 selected reservations? This releases their devices and cannot be undone. " +
        "3 will be skipped: 2 already finished, 1 not yours.",
    );
  });

  it("describeSkipped uses a fixed reason order", () => {
    const p = partitionSelection(
      "release",
      [res("1", "ACTIVE", OTHER), res("2", "PENDING"), res("3", "FAILED")],
      OWNER,
    );
    expect(describeSkipped(p.skipped)).toBe("1 already finished, 1 not active, 1 not yours");
  });

  it("explains a disabled action in plain words", () => {
    const p = partitionSelection("release", [res("1", "PENDING"), res("2", "CANCELLED")], OWNER);
    expect(noneEligibleMessage("release", p.skipped)).toBe(
      "None of the selected reservations can be released: 1 already finished, 1 not active.",
    );
    const c = partitionSelection("cancel", [res("1", "CANCELLED")], OWNER);
    expect(noneEligibleMessage("cancel", c.skipped)).toBe(
      "None of the selected reservations can be cancelled: 1 already finished.",
    );
  });
});
