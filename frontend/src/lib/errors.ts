import type { AICommitTopologyUnwireableDetail } from "@/types/ai.types";

/**
 * A HERD backend service returns error detail as either a plain string or,
 * on a pydantic validation failure, a list of error objects. Passing that
 * list straight to toast.error renders raw objects as a React child and
 * throws inside the Toaster, which sits outside the ErrorBoundary in
 * App.tsx and blanks the app. Only ever surface a string; anything else
 * (a list, undefined, a non-string) falls back to the caller-supplied
 * message.
 *
 * Single-sourced here (previously duplicated across HypervisorsPage,
 * GrantsPage, RecipeDraftPanel, api/reservations.ts, and api/topologies.ts)
 * so the guard cannot silently regress in a new call site.
 */
export function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : fallback;
}

/**
 * Narrows an axios error's response `detail` to a structured, object-shaped
 * body carrying a given HTTP status (review fix F9, issue #34): the shared
 * "is this the shape I think it is" plumbing behind `forkConflictDetail`,
 * `forkDeviceNotMemberDetail`, and every ADR 0014 L3 narrower in
 * api/reservations.ts, none of which previously agreed on how they guarded
 * `response`/`detail` being present. `matches` gets the detail object once
 * it is confirmed to be one (never called otherwise) and does the caller's
 * own discriminant check, since not every structured detail shares one field
 * name: the port-claim conflict body has no `error` key at all (only
 * `message` + `conflicts`), while every ADR 0014 body keys on `error`.
 * Returns null for any status mismatch, a non-object detail, or a `matches`
 * failure, so callers keep their existing "try each narrower in turn" style.
 */
export function structuredDetail<T>(
  err: unknown,
  status: number,
  matches: (detail: Record<string, unknown>) => boolean,
): T | null {
  const response = (err as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response;
  if (response?.status !== status) return null;
  const detail = response.data?.detail;
  if (detail && typeof detail === "object" && matches(detail as Record<string, unknown>)) {
    return detail as T;
  }
  return null;
}

/**
 * Narrows an axios error's response detail to the structured commit-time
 * wireability 422 (commit-side fail-fast hardening): an AI proposal whose
 * canvas save succeeded but has an edge with no physical cable path between
 * the two devices. Returns null for any other shape (a plain-string 422,
 * a different status, or a 422 for some other reason), so the caller keeps
 * the existing generic "Commit failed: <detail>" toast as its fallback.
 * Same shape and style as the fork narrowers in api/reservations.ts
 * (forkConflictDetail, forkDeviceNotMemberDetail, and the ADR 0014 L3
 * narrowers), kept here instead since this detail belongs to the AI commit
 * flow, not a fork.
 */
export function aiCommitTopologyUnwireableDetail(
  err: unknown,
): AICommitTopologyUnwireableDetail | null {
  return structuredDetail<AICommitTopologyUnwireableDetail>(
    err,
    422,
    (d) => d.error === "topology_unwireable" && Array.isArray(d.invalid_edges),
  );
}

/**
 * One role pair the AI topology generator could not wire, from the
 * `topology_unconnectable` 422 body. `source_template`/`target_template` name
 * the templates behind those roles, which is what an operator needs to act on
 * (the lab has nothing cabled between devices of those two kinds).
 */
export interface UnconnectableRolePair {
  source_role: string;
  target_role: string;
  source_template: string;
  target_template: string;
}

export interface TopologyUnconnectableDetail {
  error: "topology_unconnectable";
  pairs: UnconnectableRolePair[];
  message: string;
}

/**
 * Narrows an axios error's response detail to the AI generator's structured
 * unconnectable-topology 422: the proposal had at least one edge no available
 * pair of devices can carry, so generation failed rather than returning a
 * topology that could not be reserved. Returns null for any other shape, so a
 * caller keeps its plain-string fallback for every other 4xx.
 */
export function topologyUnconnectableDetail(err: unknown): TopologyUnconnectableDetail | null {
  return structuredDetail<TopologyUnconnectableDetail>(
    err,
    422,
    (d) => d.error === "topology_unconnectable" && Array.isArray(d.pairs),
  );
}

/**
 * Render the detail as the text of one error toast: the server's sentence,
 * then one "source role to target role" line per unwireable pair.
 */
export function formatUnconnectableDetail(detail: TopologyUnconnectableDetail): string {
  const lines = detail.pairs.map((pair) => `${pair.source_role} to ${pair.target_role}`);
  const message = detail.message || "The proposed topology cannot be wired in this lab.";
  return lines.length > 0 ? `${message}\n${lines.join("\n")}` : message;
}
