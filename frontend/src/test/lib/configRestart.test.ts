import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LastRestart, RestartResult } from "@/api/config";
import {
  RESTART_NOT_STARTED_GRACE_MS,
  RESTART_POLL_INTERVAL_MS,
  RESTART_WAIT_MS,
  answerWasLost,
  waitForRestart,
  type RestartOutcome,
} from "@/lib/configRestart";

// Issue #1433: the verdict on a restart whose POST /apply answer was lost comes from
// the config service's record of the run (GET /apply/last), read with a bounded wait.

const RESULT: RestartResult = { restarted: ["acl", "auth"], errors: [] };

function axiosLike(status: number | null, data?: unknown): unknown {
  return status === null
    ? { message: "Network Error", code: "ERR_NETWORK" }
    : { message: "Request failed", response: { status, data } };
}

describe("answerWasLost (issue #1433)", () => {
  it.each([
    ["no response (ERR_NETWORK_CHANGED, reset, timeout)", axiosLike(null), true],
    ["a 502 from the gateway", axiosLike(502, "Bad Gateway"), true],
    ["a 503 from the gateway", axiosLike(503, "Service Unavailable"), true],
    ["a 504 from the gateway", axiosLike(504, ""), true],
    ["a gateway status with an object body", axiosLike(502, { message: "x" }), true],
    ["a gateway status with no body", axiosLike(503), true],
    [
      "config's own 503 with its detail",
      axiosLike(503, { detail: "The config password store is unusable; the config service log names the cause" }),
      false,
    ],
    ["400 No configuration to apply", axiosLike(400, { detail: "No configuration to apply" }), false],
    ["401", axiosLike(401, { detail: "Not authenticated" }), false],
    ["403 rotate first", axiosLike(403, { detail: "Change the config password first" }), false],
    ["404", axiosLike(404, { detail: "Not Found" }), false],
    ["500", axiosLike(500, "Internal Server Error"), false],
    ["a response with no status", { response: {} }, false],
    ["null", null, true],
    ["a plain Error", new Error("boom"), true],
  ])("%s", (_name, err, lost) => {
    expect(answerWasLost(err)).toBe(lost);
  });
});

describe("waitForRestart (issue #1433)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  type Step = LastRestart | { error: unknown };

  /** A read that answers each scripted step in turn, then repeats the last one. */
  function scripted(steps: Step[]) {
    let calls = 0;
    const read = vi.fn(async () => {
      const step = steps[Math.min(calls, steps.length - 1)];
      calls += 1;
      if ("error" in step) throw step.error;
      return step;
    });
    return read;
  }

  const none: LastRestart = { id: null, state: "none", result: null };
  const prior = (state: "running" | "done" = "done"): LastRestart => ({
    id: "a".repeat(32),
    state,
    result: state === "done" ? { restarted: [], errors: ["old"] } : null,
  });
  const ours = (state: "running" | "done", result = RESULT): LastRestart => ({
    id: "b".repeat(32),
    state,
    result: state === "done" ? result : null,
  });
  const lost = { error: axiosLike(null) };

  async function settle(promise: Promise<RestartOutcome>, ms: number): Promise<RestartOutcome> {
    let outcome: RestartOutcome | undefined;
    void promise.then((o) => (outcome = o));
    await vi.advanceTimersByTimeAsync(ms);
    expect(outcome, "the wait did not end in the time given").toBeDefined();
    return outcome as RestartOutcome;
  }

  it("is done at once when the first read shows the new run finished", async () => {
    const read = scripted([ours("done")]);
    const outcome = await settle(waitForRestart(read, prior().id), 0);
    expect(outcome).toEqual({ kind: "done", result: RESULT });
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("waits through running reads and lost reads, then is done", async () => {
    const read = scripted([lost, ours("running"), lost, ours("running"), ours("done")]);
    const outcome = await settle(waitForRestart(read, prior().id), 4 * RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "done", result: RESULT });
    expect(read).toHaveBeenCalledTimes(5);
  });

  it("passes a finished run's errors through as its result", async () => {
    const result = { restarted: ["auth"], errors: ["Failed to restart acl: DockerAPIError"] };
    const read = scripted([ours("done", result)]);
    expect(await settle(waitForRestart(read, null), 0)).toEqual({ kind: "done", result });
  });

  it("never takes the run that was there before the apply for this one", async () => {
    const read = scripted([prior("done")]);
    const outcome = await settle(
      waitForRestart(read, prior().id),
      RESTART_NOT_STARTED_GRACE_MS,
    );
    expect(outcome).toEqual({ kind: "not_started" });
  });

  it.each([
    ["no run recorded at all", none, null],
    ["only the earlier run", prior("done"), "a".repeat(32)],
  ])("is not_started after the grace when the record shows %s", async (_name, record, priorId) => {
    const read = scripted([record]);
    const promise = waitForRestart(read, priorId);
    let outcome: RestartOutcome | undefined;
    void promise.then((o) => (outcome = o));
    await vi.advanceTimersByTimeAsync(RESTART_NOT_STARTED_GRACE_MS - 1);
    expect(outcome).toBeUndefined();
    await vi.advanceTimersByTimeAsync(RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "not_started" });
  });

  it("takes a run that appears within the grace", async () => {
    const read = scripted([none, none, ours("running"), ours("done")]);
    const outcome = await settle(waitForRestart(read, null), 3 * RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "done", result: RESULT });
  });

  it("judges not_started only on a read that answered", async () => {
    // Lost reads past the grace say nothing about the run; the next answer decides.
    const steps: Step[] = Array(8).fill(lost);
    const read = scripted([...steps, ours("done")]);
    const outcome = await settle(waitForRestart(read, null), 8 * RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "done", result: RESULT });
  });

  it("is timed_out after the whole wait while the run is still running", async () => {
    const read = scripted([ours("running")]);
    const promise = waitForRestart(read, prior().id);
    let outcome: RestartOutcome | undefined;
    void promise.then((o) => (outcome = o));
    await vi.advanceTimersByTimeAsync(RESTART_WAIT_MS - RESTART_POLL_INTERVAL_MS);
    expect(outcome).toBeUndefined();
    await vi.advanceTimersByTimeAsync(RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "timed_out" });
    // Bounded: one read per interval, never more.
    expect(read.mock.calls.length).toBe(RESTART_WAIT_MS / RESTART_POLL_INTERVAL_MS + 1);
  });

  it("is timed_out after the whole wait when no read ever answers", async () => {
    const read = scripted([lost]);
    const outcome = await settle(waitForRestart(read, null), RESTART_WAIT_MS);
    expect(outcome).toEqual({ kind: "timed_out" });
  });

  it.each([
    ["401, a session that ended", axiosLike(401, { detail: "Not authenticated" })],
    ["404, no such route", axiosLike(404, { detail: "Not Found" })],
    ["500", axiosLike(500, "Internal Server Error")],
  ])("is unreadable at once on a refused read: %s", async (_name, error) => {
    const read = scripted([ours("running"), { error }]);
    const outcome = await settle(waitForRestart(read, null), RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "unreadable" });
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("is cancelled when the signal aborts, and reads no more", async () => {
    const read = scripted([ours("running")]);
    const life = new AbortController();
    const promise = waitForRestart(read, null, { signal: life.signal });
    await vi.advanceTimersByTimeAsync(RESTART_POLL_INTERVAL_MS);
    const before = read.mock.calls.length;
    life.abort();
    const outcome = await settle(promise, RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "cancelled" });
    expect(read.mock.calls.length).toBe(before);
  });

  it("keeps the wait above the config service's 720 s wait for the restarter", () => {
    expect(RESTART_WAIT_MS).toBeGreaterThan(720_000);
    expect(RESTART_NOT_STARTED_GRACE_MS).toBeLessThan(RESTART_WAIT_MS);
  });
});
