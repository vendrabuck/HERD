import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import {
  STALE_RUNNING_THRESHOLD_MS,
  isRunStale,
  runsRefetchInterval,
} from "@/api/ldapSync";
import type { LdapSyncRun } from "@/types/ldapSync.types";

const NOW = new Date("2026-08-15T12:00:00Z").getTime();

// runsRefetchInterval reads Date.now() through isRunStale, so the clock must
// be frozen at NOW: with a live clock the "1 second ago" fixtures age past
// the 30 minute stale threshold and the polling assertions flip.
beforeAll(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterAll(() => {
  vi.useRealTimers();
});

function run(overrides: Partial<LdapSyncRun>): LdapSyncRun {
  return {
    id: "r1",
    started_at: "2026-08-15T11:59:00Z",
    finished_at: null,
    trigger: "manual",
    status: "running",
    users_provisioned: 0,
    members_added: 0,
    members_removed: 0,
    members_skipped: 0,
    users_deactivated: 0,
    users_reactivated: 0,
    detail: {},
    error: null,
    ...overrides,
  };
}

describe("isRunStale", () => {
  it("is false for a non-running status regardless of age", () => {
    const old = new Date(NOW - STALE_RUNNING_THRESHOLD_MS - 1000).toISOString();
    expect(isRunStale(run({ status: "success", started_at: old }), NOW)).toBe(false);
  });

  it("is false for a running row within the staleness threshold", () => {
    const recent = new Date(NOW - STALE_RUNNING_THRESHOLD_MS + 1000).toISOString();
    expect(isRunStale(run({ status: "running", started_at: recent }), NOW)).toBe(false);
  });

  it("is true for a running row older than the staleness threshold", () => {
    const old = new Date(NOW - STALE_RUNNING_THRESHOLD_MS - 1000).toISOString();
    expect(isRunStale(run({ status: "running", started_at: old }), NOW)).toBe(true);
  });

  it("is false exactly at the threshold boundary (strict exceeds)", () => {
    const boundary = new Date(NOW - STALE_RUNNING_THRESHOLD_MS).toISOString();
    expect(isRunStale(run({ status: "running", started_at: boundary }), NOW)).toBe(false);
  });
});

describe("runsRefetchInterval", () => {
  it("returns false when there is no page data yet", () => {
    expect(runsRefetchInterval(undefined)).toBe(false);
  });

  it("returns false when no run on the page is running", () => {
    const page = {
      items: [run({ status: "success" }), run({ id: "r2", status: "failed" })],
      total: 2,
      skip: 0,
      limit: 50,
    };
    expect(runsRefetchInterval(page)).toBe(false);
  });

  it("polls every 2s while a run is actively running", () => {
    const page = {
      items: [run({ status: "running", started_at: new Date(NOW - 1000).toISOString() })],
      total: 1,
      skip: 0,
      limit: 50,
    };
    expect(runsRefetchInterval(page)).toBe(2000);
  });

  it("stops polling once the only running row is stale", () => {
    // runsRefetchInterval calls isRunStale with no explicit `now`, which
    // defaults to the REAL Date.now(); anchor `stale` off the real clock
    // (not the fixed NOW fixture above) so this holds regardless of when
    // the suite runs.
    const stale = new Date(Date.now() - STALE_RUNNING_THRESHOLD_MS - 1000).toISOString();
    const page = {
      items: [run({ status: "running", started_at: stale })],
      total: 1,
      skip: 0,
      limit: 50,
    };
    expect(runsRefetchInterval(page)).toBe(false);
  });

  it("keeps polling if ANY run is actively running even when another is stale", () => {
    const stale = new Date(Date.now() - STALE_RUNNING_THRESHOLD_MS - 1000).toISOString();
    const page = {
      items: [
        run({ id: "r-stale", status: "running", started_at: stale }),
        run({ id: "r-fresh", status: "running", started_at: new Date().toISOString() }),
      ],
      total: 2,
      skip: 0,
      limit: 50,
    };
    expect(runsRefetchInterval(page)).toBe(2000);
  });
});
