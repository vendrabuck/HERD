import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LastRestart, RestartResult } from "@/api/config";
import {
  RESTART_NOT_STARTED_GRACE_MS,
  RESTART_POLL_INTERVAL_MS,
  RESTART_READ_TIMEOUT_MS,
  RESTART_WAIT_MS,
  LastRestartReadTimedOut,
  answerWasLost,
  formatFinishedAt,
  lastRestartHeading,
  readOnOpen,
  readWithin,
  waitForRestart,
  type RestartOutcome,
} from "@/lib/configRestart";

// Issue #1433: the verdict on a restart whose POST /apply answer was lost comes from
// the config service's record of the run (GET /apply/last), read with a bounded wait.

const RESULT: RestartResult = { restarted: ["acl", "auth"], errors: [] };
const FINISHED = "2026-11-27T16:51:23+00:00";

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
    ["a read given up after its timeout", new LastRestartReadTimedOut(), true],
  ])("%s", (_name, err, lost) => {
    expect(answerWasLost(err)).toBe(lost);
  });
});

type Step = LastRestart | { error: unknown } | "hang";

/**
 * A read that answers each scripted step in turn, then repeats the last one. "hang"
 * never settles; every read records the signal it was given.
 */
function scripted(steps: Step[]) {
  let calls = 0;
  const signals: (AbortSignal | undefined)[] = [];
  const read = vi.fn((signal?: AbortSignal) => {
    const step = steps[Math.min(calls, steps.length - 1)];
    calls += 1;
    signals.push(signal);
    if (step === "hang") return new Promise<LastRestart>(() => {});
    if ("error" in step) return Promise.reject(step.error);
    return Promise.resolve(step);
  });
  return { read, signals };
}

const none: LastRestart = { id: null, state: "none", result: null, finished_at: null };
const prior = (state: "running" | "done" = "done"): LastRestart => ({
  id: "a".repeat(32),
  state,
  result: state === "done" ? { restarted: [], errors: ["old"] } : null,
  finished_at: state === "done" ? "2026-11-20T10:00:00+00:00" : null,
});
const ours = (state: "running" | "done", result = RESULT): LastRestart => ({
  id: "b".repeat(32),
  state,
  result: state === "done" ? result : null,
  finished_at: state === "done" ? FINISHED : null,
});
const lost = { error: axiosLike(null) };

async function settle<T>(promise: Promise<T>, ms: number): Promise<T> {
  let outcome: T | undefined;
  let settled = false;
  void promise.then((o) => {
    outcome = o;
    settled = true;
  });
  await vi.advanceTimersByTimeAsync(ms);
  expect(settled, "the wait did not end in the time given").toBe(true);
  return outcome as T;
}

describe("waitForRestart (issue #1433)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("is done at once when the first read shows the new run finished", async () => {
    const { read } = scripted([ours("done")]);
    const outcome = await settle(waitForRestart(read, prior().id), 0);
    expect(outcome).toEqual({ kind: "done", result: RESULT, finishedAt: FINISHED });
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("waits through running reads and lost reads, then is done", async () => {
    const { read } = scripted([lost, ours("running"), lost, ours("running"), ours("done")]);
    const outcome = await settle(waitForRestart(read, prior().id), 4 * RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "done", result: RESULT, finishedAt: FINISHED });
    expect(read).toHaveBeenCalledTimes(5);
  });

  it("reads the record every 2 seconds", async () => {
    // A literal, not the constant: a changed interval must fail here.
    expect(RESTART_POLL_INTERVAL_MS).toBe(2_000);
    const { read } = scripted([ours("running")]);
    void waitForRestart(read, null);
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_999);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(read).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(read).toHaveBeenCalledTimes(3);
  });

  it("passes a finished run's errors through as its result", async () => {
    const result = { restarted: ["auth"], errors: ["Failed to restart acl: DockerAPIError"] };
    const { read } = scripted([ours("done", result)]);
    expect(await settle(waitForRestart(read, null), 0)).toEqual({
      kind: "done",
      result,
      finishedAt: FINISHED,
    });
  });

  it("never takes the run that was there before the apply for this one", async () => {
    const { read } = scripted([prior("done")]);
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
    const { read } = scripted([record]);
    const promise = waitForRestart(read, priorId);
    let outcome: RestartOutcome | undefined;
    void promise.then((o) => (outcome = o));
    await vi.advanceTimersByTimeAsync(RESTART_NOT_STARTED_GRACE_MS - 1);
    expect(outcome).toBeUndefined();
    await vi.advanceTimersByTimeAsync(RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "not_started" });
  });

  it("takes a run that appears within the grace", async () => {
    const { read } = scripted([none, none, ours("running"), ours("done")]);
    const outcome = await settle(waitForRestart(read, null), 3 * RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "done", result: RESULT, finishedAt: FINISHED });
  });

  it("judges not_started only on a read that answered", async () => {
    // Lost reads past the grace say nothing about the run; the next answer decides.
    const steps: Step[] = Array(8).fill(lost);
    const { read } = scripted([...steps, ours("done")]);
    const outcome = await settle(waitForRestart(read, null), 8 * RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "done", result: RESULT, finishedAt: FINISHED });
  });

  it("is timed_out after the whole wait while the run is still running", async () => {
    const { read } = scripted([ours("running")]);
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
    const { read } = scripted([lost]);
    const outcome = await settle(waitForRestart(read, null), RESTART_WAIT_MS);
    expect(outcome).toEqual({ kind: "timed_out" });
  });

  it("gives a read up after 10 seconds, aborts it, and keeps polling", async () => {
    expect(RESTART_READ_TIMEOUT_MS).toBe(10_000);
    const { read, signals } = scripted(["hang", ours("done")]);
    const promise = waitForRestart(read, null);
    let outcome: RestartOutcome | undefined;
    void promise.then((o) => (outcome = o));
    await vi.advanceTimersByTimeAsync(9_999);
    expect(outcome).toBeUndefined();
    expect(signals[0]?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(signals[0]?.aborted).toBe(true);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(outcome).toEqual({ kind: "done", result: RESULT, finishedAt: FINISHED });
  });

  it("stays bounded when every read hangs", async () => {
    const { read } = scripted(["hang"]);
    const promise = waitForRestart(read, null);
    let outcome: RestartOutcome | undefined;
    void promise.then((o) => (outcome = o));
    await vi.advanceTimersByTimeAsync(RESTART_WAIT_MS - 1);
    expect(outcome).toBeUndefined();
    // At most one read past the wait, and that read is given up after its timeout.
    await vi.advanceTimersByTimeAsync(RESTART_READ_TIMEOUT_MS + 1);
    expect(outcome).toEqual({ kind: "timed_out" });
  });

  it.each([
    ["401, a session that ended", axiosLike(401, { detail: "Not authenticated" })],
    ["404, no such route", axiosLike(404, { detail: "Not Found" })],
    ["500", axiosLike(500, "Internal Server Error")],
  ])("is unreadable at once on a refused read: %s", async (_name, error) => {
    const { read } = scripted([ours("running"), { error }]);
    const outcome = await settle(waitForRestart(read, null), RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "unreadable" });
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("is cancelled when the signal aborts, and reads no more", async () => {
    const { read } = scripted([ours("running")]);
    const life = new AbortController();
    const promise = waitForRestart(read, null, { signal: life.signal });
    await vi.advanceTimersByTimeAsync(RESTART_POLL_INTERVAL_MS);
    const before = read.mock.calls.length;
    life.abort();
    const outcome = await settle(promise, RESTART_POLL_INTERVAL_MS);
    expect(outcome).toEqual({ kind: "cancelled" });
    expect(read.mock.calls.length).toBe(before);
  });

  it("is cancelled when the signal aborts during a read that then shows the run done", async () => {
    let answer: (value: LastRestart) => void = () => {};
    const read = vi.fn(() => new Promise<LastRestart>((resolve) => (answer = resolve)));
    const life = new AbortController();
    const promise = waitForRestart(read, null, { signal: life.signal });
    await vi.advanceTimersByTimeAsync(0);
    life.abort();
    answer(ours("done"));
    expect(await settle(promise, 0)).toEqual({ kind: "cancelled" });
  });

  it("keeps the wait above the config service's 720 s wait for the restarter", () => {
    expect(RESTART_WAIT_MS).toBeGreaterThan(720_000);
    expect(RESTART_NOT_STARTED_GRACE_MS).toBeLessThan(RESTART_WAIT_MS);
  });
});

describe("readWithin and readOnOpen (issue #1433)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("readWithin answers the record and passes the read a live signal", async () => {
    const { read, signals } = scripted([ours("done")]);
    expect(await settle(readWithin(read), 0)).toEqual(ours("done"));
    expect(signals[0]?.aborted).toBe(false);
  });

  it("readWithin fails with LastRestartReadTimedOut and aborts the read after 10 seconds", async () => {
    const { read, signals } = scripted(["hang"]);
    let failure: unknown;
    void readWithin(read).catch((err) => (failure = err));
    await vi.advanceTimersByTimeAsync(9_999);
    expect(failure).toBeUndefined();
    await vi.advanceTimersByTimeAsync(1);
    expect(failure).toBeInstanceOf(LastRestartReadTimedOut);
    expect(signals[0]?.aborted).toBe(true);
  });

  it("readOnOpen answers the record while the page is there", async () => {
    const { read } = scripted([ours("running")]);
    expect(await settle(readOnOpen(read, new AbortController().signal), 0)).toEqual(
      ours("running"),
    );
  });

  it.each([
    ["a lost read", lost as Step],
    ["a refused read", { error: axiosLike(401, { detail: "Not authenticated" }) } as Step],
  ])("readOnOpen answers null for %s", async (_name, step) => {
    const { read } = scripted([step]);
    expect(await settle(readOnOpen(read, new AbortController().signal), 0)).toBeNull();
  });

  it("readOnOpen answers null for a read that hangs past its timeout", async () => {
    const { read } = scripted(["hang"]);
    expect(
      await settle(readOnOpen(read, new AbortController().signal), RESTART_READ_TIMEOUT_MS),
    ).toBeNull();
  });

  it("readOnOpen answers null when the page went away while the read was in flight", async () => {
    let answer: (value: LastRestart) => void = () => {};
    const read = vi.fn(() => new Promise<LastRestart>((resolve) => (answer = resolve)));
    const life = new AbortController();
    const promise = readOnOpen(read, life.signal);
    life.abort();
    answer(ours("done"));
    expect(await settle(promise, 0)).toBeNull();
  });
});

describe("formatFinishedAt and lastRestartHeading (issue #1433)", () => {
  // Built from the same instant with the Date's own local fields, so the expectation
  // holds in any timezone the suite runs in (make test-frontend-tz runs it in Los
  // Angeles); the literal UTC fields are checked against the parse separately.
  function localText(iso: string): string {
    const at = new Date(iso);
    const two = (n: number) => String(n).padStart(2, "0");
    return `${at.getFullYear()}-${two(at.getMonth() + 1)}-${two(at.getDate())} ${two(at.getHours())}:${two(at.getMinutes())}`;
  }

  it("renders the recorded UTC instant in the viewer's own timezone", () => {
    const at = new Date(FINISHED);
    expect(at.getUTCHours()).toBe(16);
    expect(at.getUTCMinutes()).toBe(51);
    expect(formatFinishedAt(FINISHED)).toBe(localText(FINISHED));
    expect(formatFinishedAt(FINISHED)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  });

  it("renders two instants an hour apart an hour apart", () => {
    const later = "2026-11-27T17:51:23+00:00";
    expect(formatFinishedAt(later)).toBe(localText(later));
    expect(formatFinishedAt(later)).not.toBe(formatFinishedAt(FINISHED));
  });

  it("keeps text that does not parse as it is", () => {
    expect(formatFinishedAt("not a time")).toBe("not a time");
  });

  it("labels a run the page did not start with its finish time", () => {
    expect(lastRestartHeading(FINISHED)).toBe(`Last restart, finished ${localText(FINISHED)}`);
    expect(lastRestartHeading(null)).toBe("Last restart");
  });
});
