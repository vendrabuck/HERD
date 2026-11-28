/**
 * The four states a page that loads data can be in (issue #1320). A failed
 * fetch is never "empty": "empty" means the server answered successfully
 * with nothing in it.
 *
 * - `loading`: no data yet and no failure yet (a disabled query counts too).
 * - `error`: no data, and the last fetch failed; or data arrived in a shape
 *   the page cannot use (`isEmpty` or `isValid` threw, or `isValid` said no).
 * - `empty`: usable data arrived and `isEmpty` says it holds nothing.
 * - `loaded`: usable data arrived with something in it.
 *
 * Data that already arrived wins over a later failure: when a background
 * refetch fails, TanStack Query keeps the last good data beside the error,
 * and the page keeps showing it, with `refreshFailed` true so the page says
 * the refresh failed (`LoadErrorNotice`). Placeholder data (`keepPreviousData`
 * while the next page loads) counts as data, the same way the pages rendered
 * it before.
 *
 * `loadState` never throws: a predicate that throws on an unexpected shape
 * reads as `error`, so a page never reaches its rows with data it cannot read.
 */
export type LoadState = "loading" | "error" | "empty" | "loaded";

export interface LoadableQuery<T> {
  data: T | undefined;
  isError: boolean;
}

export function loadState<T>(
  query: LoadableQuery<T>,
  isEmpty: (data: T) => boolean = () => false,
  isValid: (data: T) => boolean = () => true,
): LoadState {
  if (query.data === undefined) return query.isError ? "error" : "loading";
  try {
    if (!isValid(query.data)) return "error";
    return isEmpty(query.data) ? "empty" : "loaded";
  } catch {
    return "error";
  }
}

/**
 * True when the page is showing data from an earlier answer and the latest
 * fetch of it failed (a background refetch, or a Retry that failed again).
 * Never true in the `error` state, which shows the full error instead.
 */
export function refreshFailed<T>(
  query: LoadableQuery<T>,
  isEmpty?: (data: T) => boolean,
  isValid?: (data: T) => boolean,
): boolean {
  return query.isError && loadState(query, isEmpty, isValid) !== "error";
}

/** `isEmpty` for a paginated answer: no items on this page. Throws when the
 * answer has no `items` list, which `loadState` reads as `error`. */
export function noItems(page: { items: unknown[] }): boolean {
  if (!Array.isArray(page?.items)) throw new TypeError("a paginated answer has no items list");
  return page.items.length === 0;
}

/** `isEmpty` for a plain list answer. Throws when the answer is not a list. */
export function noEntries(list: unknown[]): boolean {
  if (!Array.isArray(list)) throw new TypeError("a list answer is not a list");
  return list.length === 0;
}

/** `isValid` for an answer that must be a JSON object (not a list, not text). */
export function isPlainObject(data: unknown): boolean {
  return typeof data === "object" && data !== null && !Array.isArray(data);
}
