import { isAxiosError } from "axios";
import type { AICommitTopologyUnwireableDetail } from "@/types/ai.types";
import { isUnreadableAnswer } from "@/api/answerGuard";

/**
 * The raw `detail` of an axios error's response body, typed `unknown` on
 * purpose: the one place outside this module that reads it is a structured
 * narrower that checks the shape itself. Rendering code never calls this;
 * it calls `errorDetail` (issue #1314).
 */
export function responseDetail(err: unknown): unknown {
  return (err as { response?: { data?: { detail?: unknown } } } | null | undefined)?.response
    ?.data?.detail;
}

/** The most entries of a pydantic 422 list `detailText` names before "and N more". */
export const VALIDATION_DETAIL_MAX_ITEMS = 3;

/** The most characters (code points) `detailText` returns for a 422 list. */
export const VALIDATION_DETAIL_MAX_LENGTH = 300;

/** The most characters (code points) `detailText` returns for a plain string. */
export const STRING_DETAIL_MAX_LENGTH = 1000;

// FastAPI prefixes every validation location with where the value came from;
// the field path after it is what a person can act on.
const VALIDATION_LOCATION_SOURCES = new Set(["body", "query", "path", "header", "cookie"]);

function validationEntryText(entry: unknown): string | null {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const { loc, msg } = entry as { loc?: unknown; msg?: unknown };
  if (typeof msg !== "string" || !msg.trim()) return null;
  let where = "";
  if (
    Array.isArray(loc) &&
    loc.every((part) => typeof part === "string" || typeof part === "number")
  ) {
    // An empty or blank part names nothing; dropping it keeps `body.` and
    // `.: msg` off the screen.
    const parts = loc.map(String).filter((part) => part.trim() !== "");
    if (parts.length > 1 && VALIDATION_LOCATION_SOURCES.has(parts[0])) parts.shift();
    where = parts.join(".");
  }
  return where ? where + ": " + msg.trim() : msg.trim();
}

function boundedText(text: string, max: number): string {
  const points = Array.from(text);
  return points.length <= max ? text : points.slice(0, max - 3).join("") + "...";
}

/**
 * One line from the readable texts of a 422 list's entries: the first
 * VALIDATION_DETAIL_MAX_ITEMS joined with "; ", then " (and N more)", cut to
 * VALIDATION_DETAIL_MAX_LENGTH characters. Shared by `detailText` and
 * `profileSaveErrorText`, so neither formatter is unbounded.
 */
function joinValidationEntries(entries: string[]): string {
  const shown = entries.slice(0, VALIDATION_DETAIL_MAX_ITEMS).join("; ");
  const rest = entries.length - VALIDATION_DETAIL_MAX_ITEMS;
  return boundedText(
    rest > 0 ? shown + " (and " + rest + " more)" : shown,
    VALIDATION_DETAIL_MAX_LENGTH,
  );
}

/**
 * The ONE normalizer from an error body's `detail` to display text (issue
 * #1314). A HERD backend service answers with a plain string, or, on a
 * pydantic validation failure (FastAPI's default 422), a list of
 * {type, loc, msg, input, ctx} objects. A non-blank string passes through,
 * cut to STRING_DETAIL_MAX_LENGTH characters. A list whose every entry
 * carries a string `msg` becomes "field: message" per entry (the location's
 * non-blank parts dotted, without a leading body, query, path, header, or
 * cookie source unless that source is all there is), the first
 * VALIDATION_DETAIL_MAX_ITEMS joined with "; "
 * plus " (and N more)", cut to VALIDATION_DETAIL_MAX_LENGTH characters. The
 * `input` echo is never shown. Anything else (an empty string or list, an
 * object, a number, null, a nested list, an entry without `msg`) is the
 * caller's fallback, so the result is always a string.
 */
export function detailText(detail: unknown, fallback: string): string {
  if (typeof detail === "string") {
    return detail.trim() ? boundedText(detail, STRING_DETAIL_MAX_LENGTH) : fallback;
  }
  if (!Array.isArray(detail) || detail.length === 0) return fallback;
  const entries: string[] = [];
  for (const entry of detail) {
    const text = validationEntryText(entry);
    if (text === null) return fallback;
    entries.push(text);
  }
  return joinValidationEntries(entries);
}

/**
 * The display text of a failed request: `detailText` of the axios error's
 * response detail. Passing a raw detail to toast.error or into JSX renders an
 * object as a React child and throws (before issue #1344 that blanked the
 * whole app, header included); every error handler in the UI goes through
 * this (or a structured narrower below), and the `no-restricted-syntax` rule
 * in eslint.config.js refuses the old `detail?: string` cast and any
 * `.data.detail` read outside this module.
 *
 * Single-sourced here (previously duplicated across HypervisorsPage,
 * GrantsPage, RecipeDraftPanel, api/reservations.ts, and api/topologies.ts)
 * so the guard cannot silently regress in a new call site.
 */
export function errorDetail(err: unknown, fallback: string): string {
  return detailText(responseDetail(err), fallback);
}

/** What a failed load says when no response arrived at all (issue #1320). */
export const LOAD_FAILURE_NO_ANSWER = "The server did not answer.";

/**
 * What a failed load says when an answer arrived but could not be used
 * (issue #1320): a 2xx the client refused as not JSON (`isUnreadableAnswer`),
 * or a body of the wrong shape that made the query fail.
 */
export const LOAD_FAILURE_UNREADABLE = "The server's answer could not be read.";

/**
 * The second sentence of a failed page load (issue #1320, `LoadError`):
 *
 * - an error carrying an HTTP status: the server's `detail` through
 *   `errorDetail`, or, when the body carries none, a fixed sentence naming the
 *   status (a number axios read from the response, never body text);
 * - an axios error with no response (a network error, a refused connection, a
 *   timeout): LOAD_FAILURE_NO_ANSWER;
 * - anything else (the client's refusal of an unreadable 2xx, a body of the
 *   wrong shape, no error object at all): LOAD_FAILURE_UNREADABLE.
 */
export function loadFailureDetail(err: unknown): string {
  const status = (err as { response?: { status?: unknown } } | null | undefined)?.response
    ?.status;
  if (typeof status === "number" && Number.isInteger(status)) {
    return errorDetail(err, "The server answered with HTTP " + status + ".");
  }
  if (isAxiosError(err) && !isUnreadableAnswer(err)) return LOAD_FAILURE_NO_ANSWER;
  return LOAD_FAILURE_UNREADABLE;
}

// Field checks for the structured narrowers (issue #1406). A narrower's
// return type is a promise to its caller that every field it names holds that
// type, and callers render those fields straight into JSX or a toast; a body
// that breaks the promise (an object `message`, a number where an id should
// be, a null list entry) would throw while rendering. So a narrower checks
// every field its type names, the list entries included, and answers null for
// a body that does not match, which sends the caller to its plain fallback. A
// field typed `unknown` is left for its caller to check.

/** A field check: true when the value has the type the field declares. */
export type FieldCheck = (value: unknown) => boolean;

export const isString: FieldCheck = (value) => typeof value === "string";
export const isStringOrNull: FieldCheck = (value) => value === null || typeof value === "string";
/** A field that may be absent; when present it is a string. */
export const isOptionalString: FieldCheck = (value) =>
  value === undefined || typeof value === "string";
export const isStringList: FieldCheck = (value) => isListOf(value, isString);

/** True for an array whose every entry passes `entry`; an empty array passes. */
export function isListOf(value: unknown, entry: FieldCheck): value is unknown[] {
  return Array.isArray(value) && value.every((item) => entry(item));
}

/**
 * True for a plain object (not null, not an array) whose own field named by
 * each key of `fields` passes that key's check. A missing field reads as
 * undefined, so only `isOptionalString` (or a check written to accept
 * undefined) lets it be absent. Fields not named are not looked at.
 */
export function hasFields(value: unknown, fields: Record<string, FieldCheck>): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return Object.keys(fields).every((key) =>
    fields[key](Object.prototype.hasOwnProperty.call(record, key) ? record[key] : undefined),
  );
}

/**
 * One cabling `InvalidRoute` entry (ADR 0014): string `node_id` and `reason`,
 * `device_id` and `detail` a string or null, `index` a whole number or null.
 * Shared by every narrower that carries an `invalid_routes` list, since the
 * editor renders `reason` and `detail` in the Routing panel.
 */
export function isInvalidRoute(value: unknown): boolean {
  return hasFields(value, {
    node_id: isString,
    device_id: isStringOrNull,
    index: (index) => index === null || Number.isInteger(index),
    reason: isString,
    detail: isStringOrNull,
  });
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
 * `message` + `conflicts`), while every ADR 0014 body keys on `error`. Since
 * issue #1406 `matches` also checks every other field `T` names (see
 * `hasFields` above). Returns null for any status mismatch, a non-object
 * detail, or a `matches` failure, so callers keep their existing "try each
 * narrower in turn" style.
 */
export function structuredDetail<T>(
  err: unknown,
  status: number,
  matches: (detail: Record<string, unknown>) => boolean,
): T | null {
  const response = (err as { response?: { status?: number } })?.response;
  if (response?.status !== status) return null;
  const detail = responseDetail(err);
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
    (d) =>
      d.error === "topology_unwireable" &&
      isString(d.message) &&
      isListOf(d.invalid_edges, (edge) =>
        hasFields(edge, {
          edge_id: isString,
          source_role: isString,
          target_role: isString,
          reason: isString,
        }),
      ),
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
    (d) =>
      d.error === "topology_unconnectable" &&
      isString(d.message) &&
      isListOf(d.pairs, (pair) =>
        hasFields(pair, {
          source_role: isString,
          target_role: isString,
          source_template: isString,
          target_template: isString,
        }),
      ),
  );
}

/**
 * One topology type in the AI generator's `topology_mixed_types` 422 body
 * (issue #1038): the roles and templates whose resolved devices are of it.
 */
export interface MixedTypeGroup {
  topology_type: string;
  roles: string[];
  templates: string[];
}

export interface TopologyMixedTypesDetail {
  error: "topology_mixed_types";
  groups: MixedTypeGroup[];
  message: string;
}

/**
 * Narrows an axios error's response detail to the AI generator's structured
 * mixed-types 422: the proposal's devices resolved to more than one topology
 * type (physical and cloud), which no topology or reservation may hold.
 * Returns null for any other shape.
 */
export function topologyMixedTypesDetail(err: unknown): TopologyMixedTypesDetail | null {
  return structuredDetail<TopologyMixedTypesDetail>(
    err,
    422,
    (d) =>
      d.error === "topology_mixed_types" &&
      isString(d.message) &&
      isListOf(d.groups, (group) =>
        hasFields(group, {
          topology_type: isString,
          roles: isStringList,
          templates: isStringList,
        }),
      ),
  );
}

/**
 * Render the mixed-types detail as one toast: the server's sentence, then one
 * "TYPE: template, template" line per type.
 */
export function formatMixedTypesDetail(detail: TopologyMixedTypesDetail): string {
  const lines = detail.groups.map(
    (group) => `${group.topology_type}: ${group.templates.join(", ")}`,
  );
  const message = detail.message || "The proposal mixes physical and cloud devices.";
  return lines.length > 0 ? `${message}\n${lines.join("\n")}` : message;
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

/**
 * The three refusals of the on-demand purpose classifier trigger that are
 * errors rather than outcomes (issue #822): 503 `purpose_classification_
 * disabled`, and the two 409s `already_suggested` and `not_eligible`.
 * Returns null for anything else (404, a 5xx without that body, a transport
 * failure) so the caller falls back to `errorDetail`.
 */
export type PurposeClassifyRefusal =
  | "purpose_classification_disabled"
  | "already_suggested"
  | "not_eligible";

export function purposeClassifyRefusal(err: unknown): PurposeClassifyRefusal | null {
  if (structuredDetail(err, 503, (d) => d.error === "purpose_classification_disabled")) {
    return "purpose_classification_disabled";
  }
  if (structuredDetail(err, 409, (d) => d.error === "already_suggested")) {
    return "already_suggested";
  }
  if (structuredDetail(err, 409, (d) => d.error === "not_eligible")) {
    return "not_eligible";
  }
  return null;
}

/**
 * Cabling's topology DELETE refusal (issue #977): 409 `topology_in_use` while
 * a PENDING, PENDING_PROVISION, or ACTIVE reservation references the topology.
 * `reservation_ids` is sorted and never empty on a real refusal.
 */
export interface TopologyInUseDetail {
  error: "topology_in_use";
  reservation_ids: string[];
}

/** Narrows a topology delete error to the structured 409; null for any other shape. */
export function topologyInUseDetail(err: unknown): TopologyInUseDetail | null {
  return structuredDetail<TopologyInUseDetail>(
    err,
    409,
    (d) =>
      d.error === "topology_in_use" &&
      Array.isArray(d.reservation_ids) &&
      d.reservation_ids.every((id) => typeof id === "string"),
  );
}

/** "In use by 2 reservations. Cancel them or wait for them to end, then delete again." */
export function formatTopologyInUse(detail: TopologyInUseDetail): string {
  const n = detail.reservation_ids.length;
  const noun = n === 1 ? "reservation" : "reservations";
  const them = n === 1 ? "it" : "them";
  return `In use by ${n} ${noun}. Cancel ${them} or wait for ${them} to end, then delete again.`;
}

/**
 * The text for a failed topology delete, shared by the row's Delete and the
 * bulk Delete selected: the in-use wording for the structured 409, else the
 * server's plain-string detail (a 403, a 404, the fail-closed 503), else the
 * caller's fallback.
 */
export function topologyDeleteErrorText(err: unknown, fallback: string): string {
  const inUse = topologyInUseDetail(err);
  return inUse ? formatTopologyInUse(inUse) : errorDetail(err, fallback);
}

/**
 * The text for a failed port delete (issue #1023). Inventory refuses to delete
 * a port that a cabling connection still names with a structured 409
 * {error: "port_cabled", connection_count, connection_ids}; toasting that
 * object would render nothing readable, so it becomes the cabled wording with
 * the count, the device delete guard's wording family. Any plain-string detail
 * (a 404, the fail-closed 503) passes through; anything else is the fallback.
 */
export function deletePortErrorText(err: unknown): string {
  const cabled = structuredDetail<{ connection_count?: unknown }>(
    err,
    409,
    (d) => d.error === "port_cabled",
  );
  if (cabled) {
    const count = cabled.connection_count;
    if (typeof count === "number" && count > 0) {
      const noun = count === 1 ? "1 connection" : count + " connections";
      return "Port is still cabled (" + noun + ") and cannot be deleted. Remove its cables first.";
    }
    return "Port is still cabled and cannot be deleted. Remove its cables first.";
  }
  return errorDetail(err, "Failed to delete port");
}

/**
 * Inventory's config-apply driver gate (issues #839, #1098): 409
 * {error: "driver_cannot_configure", connection_type, driver, message} when
 * the device's driver implements a contract with no configure method. Both
 * the immediate apply and the schedule answer it.
 */
export interface DriverCannotConfigureDetail {
  error: "driver_cannot_configure";
  connection_type?: unknown;
  driver?: unknown;
  message?: unknown;
}

/** Narrows a config apply error to the structured 409; null for any other shape. */
export function driverCannotConfigureDetail(err: unknown): DriverCannotConfigureDetail | null {
  return structuredDetail<DriverCannotConfigureDetail>(
    err,
    409,
    (d) => d.error === "driver_cannot_configure",
  );
}

/**
 * The text for a refused config apply or schedule (issue #1098): the driver
 * gate's own sentence plus the driver's name for the structured 409, else the
 * server's plain-string detail (a 403, a 404, a 422, the fail-closed 503),
 * else the caller's fallback.
 */
export function configApplyErrorText(err: unknown, fallback: string): string {
  const gate = driverCannotConfigureDetail(err);
  if (gate) {
    const driver = typeof gate.driver === "string" && gate.driver ? gate.driver : null;
    const type =
      typeof gate.connection_type === "string" && gate.connection_type
        ? gate.connection_type
        : null;
    const message =
      typeof gate.message === "string" && gate.message
        ? gate.message
        : "This device's driver" +
          (type ? " implements the " + type + " contract, which" : "") +
          " has no configure method, so a config apply cannot run.";
    return driver ? message + " (driver: " + driver + ")" : message;
  }
  return errorDetail(err, fallback);
}

/** The most reservation ids a restore refusal lists before "and N more". */
export const RESTORE_BLOCKED_LIST_LIMIT = 3;

/**
 * Inventory's restore guard (issue #337): 409 {message, reservations: [{id,
 * status, end_time}]} while another user's active reservation holds the
 * device. The body carries no `error` key, so the narrower keys on the list.
 */
export interface RestoreBlockedDetail {
  message?: unknown;
  reservations: { id?: unknown; status?: unknown; end_time?: unknown }[];
}

/** Narrows a config restore error to the structured 409; null for any other shape. */
export function restoreBlockedDetail(err: unknown): RestoreBlockedDetail | null {
  return structuredDetail<RestoreBlockedDetail>(
    err,
    409,
    (d) =>
      Array.isArray(d.reservations) &&
      d.reservations.every((r) => r !== null && typeof r === "object"),
  );
}

/**
 * "Device has active reservations; restore blocked: 1a2b3c4d (ACTIVE),
 * 5e6f7a8b (ACTIVE), 9c0d1e2f (PENDING_PROVISION) and 2 more". Each id is cut
 * to its first 8 characters, the way the rest of the UI shows reservation ids.
 */
export function formatRestoreBlocked(detail: RestoreBlockedDetail): string {
  const message =
    typeof detail.message === "string" && detail.message
      ? detail.message
      : "Device has active reservations; restore blocked";
  const entries = detail.reservations
    .filter((r) => typeof r.id === "string" && r.id)
    .map((r) => {
      const id = (r.id as string).slice(0, 8);
      return typeof r.status === "string" && r.status ? id + " (" + r.status + ")" : id;
    });
  if (entries.length === 0) return message;
  const shown = entries.slice(0, RESTORE_BLOCKED_LIST_LIMIT).join(", ");
  const rest = entries.length - RESTORE_BLOCKED_LIST_LIMIT;
  return message + ": " + shown + (rest > 0 ? " and " + rest + " more" : "");
}

/**
 * The text for a refused config restore (issue #1098): the reservation list
 * for the structured 409, else the server's plain-string detail (a 403, a
 * 404, a 422 schema refusal, the fail-closed 503), else `Restore failed`.
 */
export function configRestoreErrorText(err: unknown): string {
  const blocked = restoreBlockedDetail(err);
  return blocked ? formatRestoreBlocked(blocked) : errorDetail(err, "Restore failed");
}

/**
 * The config login's attempt limit (issue #1126, rule OPS-CONFIG-22): after
 * repeated failures `POST /api/config/login` answers 429 `Too many failed
 * login attempts; try again later` with `Retry-After` in whole seconds,
 * before the password is checked. That refusal is not a wrong password, so
 * the login form must not say it is one.
 */
export const LOGIN_LOCKED_FALLBACK = "Too many failed login attempts; try again later";

function responseHeader(headers: unknown, name: string): unknown {
  if (!headers || typeof headers !== "object") return undefined;
  // axios hands back an AxiosHeaders instance whose get() ignores case; a
  // plain object (a test double, an older adapter) is searched by hand.
  const getter = (headers as { get?: unknown }).get;
  if (typeof getter === "function") {
    return (getter as (this: unknown, n: string) => unknown).call(headers, name);
  }
  const wanted = name.toLowerCase();
  for (const [key, value] of Object.entries(headers as Record<string, unknown>)) {
    if (key.toLowerCase() === wanted) return value;
  }
  return undefined;
}

function isLoginLocked(err: unknown): boolean {
  return (err as { response?: { status?: number } })?.response?.status === 429;
}

/**
 * The wait a config login 429 asks for, in seconds: the `Retry-After` header
 * when it is a positive whole number, else null (no header, an HTTP date, a
 * malformed value, zero). Null for any answer that is not a 429.
 */
export function loginRetryAfterSeconds(err: unknown): number | null {
  if (!isLoginLocked(err)) return null;
  const raw = responseHeader(
    (err as { response?: { headers?: unknown } }).response?.headers,
    "Retry-After",
  );
  const text = typeof raw === "number" ? String(raw) : raw;
  if (typeof text !== "string" || !/^\s*\d+\s*$/.test(text)) return null;
  const seconds = Number(text.trim());
  return Number.isSafeInteger(seconds) && seconds > 0 ? seconds : null;
}

/**
 * The text for a config login 429 (issue #1126): "Too many failed login
 * attempts; try again in N seconds" when the wait is known, else the server's
 * own sentence, else the same sentence written here. Null for any answer that
 * is not a 429, so the caller keeps its wrong-password wording for a 401.
 */
export function loginRetryAfterText(err: unknown): string | null {
  if (!isLoginLocked(err)) return null;
  const seconds = loginRetryAfterSeconds(err);
  if (seconds !== null) {
    return (
      "Too many failed login attempts; try again in " +
      seconds +
      (seconds === 1 ? " second" : " seconds")
    );
  }
  return errorDetail(err, LOGIN_LOCKED_FALLBACK);
}

/** The config change-password toast for every failure but a refused password. */
export const CONFIG_CHANGE_PASSWORD_FALLBACK = "Failed to change password";

/**
 * The text for a failed config `POST /change-password` (issue #1452). A 422
 * whose detail is a string is the server's refusal of the new password
 * (OPS-CONFIG-8): one of its fixed sentences (`Password must be at least 8
 * characters`, `... at most 32 characters`, `... must not contain a NUL
 * character`, `... must be valid UTF-8 text`), which never hold the submitted
 * value, so it is shown as is. Anything else, a 422 validation list
 * (OPS-CONFIG-30, whose `msg` could quote part of a value), the 503 for an
 * unusable password store, a 401, a 5xx, or no answer, is
 * CONFIG_CHANGE_PASSWORD_FALLBACK.
 */
export function configChangePasswordErrorText(err: unknown): string {
  const status = (err as { response?: { status?: number } } | null | undefined)?.response?.status;
  const detail = responseDetail(err);
  if (status !== 422 || typeof detail !== "string") return CONFIG_CHANGE_PASSWORD_FALLBACK;
  return detailText(detail, CONFIG_CHANGE_PASSWORD_FALLBACK);
}

/**
 * The text for a failed bulk import (issue #1575). A 413 (the upload is over
 * the import bound) or a 422 (the file is not UTF-8, not readable JSON, or not
 * the expected shape) is the server's refusal of the file itself, in fixed
 * copy written for the user to act on, so its detail is shown through
 * `errorDetail`, and a body with no usable detail is the fallback. Any other
 * failure (a 403, a 503 from an upstream lookup, a 5xx, no answer at all) is
 * not about the file, and is the fallback whatever its detail says.
 */
export function importErrorText(err: unknown, fallback: string): string {
  const status = (err as { response?: { status?: unknown } } | null | undefined)?.response?.status;
  if (status !== 413 && status !== 422) return fallback;
  return errorDetail(err, fallback);
}

// Field labels for the profile 422 list (issue #1252), matching the labels on
// the Settings Profile section and the Users page Edit profile dialog.
const PROFILE_FIELD_LABELS: Record<string, string> = {
  full_name: "Name",
  contact_email: "Contact email",
  phone: "Phone",
};

// Pydantic reports the phone character rule as the raw regular expression;
// the profile forms show the rule in words instead (issue #1252).
export const PHONE_PATTERN_TEXT =
  "Phone may contain only digits, spaces, +, -, (, and ), with at least one digit";

/**
 * Toast text for a failed profile save (issue #1252). A plain string detail
 * (404 "User not found") goes through `detailText` (bounded; blank falls
 * back). A pydantic 422 list naming the profile fields becomes one
 * "<Label>: <message>" per field, joined and bounded the way `detailText`
 * joins a list (issue #1314), so a bad phone and a bad address are both
 * reported; a phone that breaks the character rule reads PHONE_PATTERN_TEXT,
 * not the regex. Anything else, a 422 entry for another field included,
 * falls back to `fallback`.
 */
export function profileSaveErrorText(err: unknown, fallback: string): string {
  const detail = responseDetail(err);
  if (typeof detail === "string") return detailText(detail, fallback);
  if (!Array.isArray(detail) || detail.length === 0) return fallback;
  const parts: string[] = [];
  for (const entry of detail) {
    const loc = (entry as { loc?: unknown })?.loc;
    const msg = (entry as { msg?: unknown })?.msg;
    const field = Array.isArray(loc) ? loc[loc.length - 1] : undefined;
    const label = typeof field === "string" ? PROFILE_FIELD_LABELS[field] : undefined;
    if (!label || typeof msg !== "string") return fallback;
    const type = (entry as { type?: unknown })?.type;
    if (field === "phone" && type === "string_pattern_mismatch") {
      parts.push(PHONE_PATTERN_TEXT);
    } else {
      parts.push(`${label}: ${msg}`);
    }
  }
  return joinValidationEntries(parts);
}
