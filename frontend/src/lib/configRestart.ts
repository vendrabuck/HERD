import type { LastRestart, RestartResult } from "@/api/config";

// Issue #1433: Save and Restart's verdict comes from whether the restart finished,
// not from whether the one POST /apply survived it. Restarting a container on a
// Linux Docker host adds and removes a host interface address, and a browser on that
// host aborts every request in flight (Chromium's ERR_NETWORK_CHANGED), so the page
// can lose the answer of a restart that went fine. The config service keeps its last
// restart (GET /apply/last); the page reads it back when the answer is lost.

/** Between two reads of the last restart. */
export const RESTART_POLL_INTERVAL_MS = 2_000;
/** How long a readable record that shows no new run is given to show one. */
export const RESTART_NOT_STARTED_GRACE_MS = 10_000;
/**
 * The whole wait for a result once the answer is lost: above the config service's
 * 720 s wait for the restarter (OPS-CONFIG-28), so a slow restart still reports.
 */
export const RESTART_WAIT_MS = 750_000;

/** Gateway statuses that say the request did not get an answer from config. */
const GATEWAY_STATUSES = new Set([502, 503, 504]);

/**
 * True when a config request failed without an answer from the config service: no
 * response at all (a network error, ERR_NETWORK_CHANGED, a reset connection, a
 * timeout), or a 502, 503, or 504 from the gateway. The config service's own answers
 * carry a JSON `detail` (its 503s included), so a gateway status with a string
 * `detail` is config's own refusal, not a lost answer.
 */
export function answerWasLost(err: unknown): boolean {
  const response = (err as { response?: { status?: number; data?: unknown } } | null)
    ?.response;
  if (!response) return true;
  if (response.status === undefined || !GATEWAY_STATUSES.has(response.status)) return false;
  const detail = (response.data as { detail?: unknown } | null | undefined)?.detail;
  return typeof detail !== "string";
}

export type RestartOutcome =
  /** The run finished; its result is the one POST /apply answered. */
  | { kind: "done"; result: RestartResult }
  /** The config service answered and holds no new run: the apply never started one. */
  | { kind: "not_started" }
  /** No finished run within RESTART_WAIT_MS. */
  | { kind: "timed_out" }
  /** The config service refused the read (a session that ended, a missing route). */
  | { kind: "unreadable" }
  /** The caller stopped waiting (the page went away). */
  | { kind: "cancelled" };

export interface WaitOptions {
  intervalMs?: number;
  notStartedGraceMs?: number;
  waitMs?: number;
  signal?: AbortSignal;
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Read the last restart until the run started after `priorId` has finished.
 *
 * `priorId` is the record's id read before the apply was sent (null when there was
 * none), so an older run is never taken for this one. A read that is itself lost
 * (answerWasLost) is retried; any other refusal ends the wait as `unreadable`. The
 * wait is bounded: `not_started` once a readable record has shown no new run for
 * `notStartedGraceMs`, `timed_out` after `waitMs`.
 */
export async function waitForRestart(
  read: () => Promise<LastRestart>,
  priorId: string | null,
  {
    intervalMs = RESTART_POLL_INTERVAL_MS,
    notStartedGraceMs = RESTART_NOT_STARTED_GRACE_MS,
    waitMs = RESTART_WAIT_MS,
    signal,
  }: WaitOptions = {},
): Promise<RestartOutcome> {
  const started = Date.now();
  for (;;) {
    if (signal?.aborted) return { kind: "cancelled" };
    let last: LastRestart | null = null;
    try {
      last = await read();
    } catch (err) {
      if (!answerWasLost(err)) return { kind: "unreadable" };
    }
    if (signal?.aborted) return { kind: "cancelled" };
    const elapsed = Date.now() - started;
    if (last) {
      const isNewRun = last.id !== null && last.id !== priorId;
      if (isNewRun && last.state === "done" && last.result) {
        return { kind: "done", result: last.result };
      }
      if (!isNewRun && elapsed >= notStartedGraceMs) return { kind: "not_started" };
    }
    if (elapsed >= waitMs) return { kind: "timed_out" };
    await pause(intervalMs);
  }
}

/** The page's sentence for each way a lost answer can end without a result. */
export const RESTART_NOT_STARTED_TEXT = "The restart did not start; try Save and Restart again";
export const RESTART_TIMED_OUT_TEXT =
  "The connection dropped during the restart and no result arrived in time; check service health";
export const RESTART_UNREADABLE_TEXT =
  "The connection dropped during the restart and its result could not be read; check service health";
/** A restart running when the editor opened that ended without a result to show. */
export const RESTART_FOLLOW_LOST_TEXT =
  "The restart that was running did not report a result; check service health";
