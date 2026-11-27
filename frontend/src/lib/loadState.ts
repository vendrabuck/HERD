/**
 * The four states a page that loads data can be in (issue #1320). A failed
 * fetch is never "empty": "empty" means the server answered successfully
 * with nothing in it.
 *
 * - `loading`: no data yet and no failure yet (a disabled query counts too).
 * - `error`: no data, and the last fetch failed.
 * - `empty`: data arrived and `isEmpty` says it holds nothing.
 * - `loaded`: data arrived with something in it.
 *
 * Data that already arrived wins over a later failure: when a background
 * refetch fails, TanStack Query keeps the last good data beside the error,
 * and the page keeps showing what the server last said rather than hiding it.
 * Placeholder data (`keepPreviousData` while the next page loads) counts as
 * data, the same way the pages rendered it before.
 */
export type LoadState = "loading" | "error" | "empty" | "loaded";

export interface LoadableQuery<T> {
  data: T | undefined;
  isError: boolean;
}

export function loadState<T>(
  query: LoadableQuery<T>,
  isEmpty: (data: T) => boolean = () => false,
): LoadState {
  if (query.data === undefined) return query.isError ? "error" : "loading";
  return isEmpty(query.data) ? "empty" : "loaded";
}

/** `isEmpty` for a paginated answer: no items on this page. */
export function noItems(page: { items: unknown[] }): boolean {
  return page.items.length === 0;
}

/** `isEmpty` for a plain list answer. */
export function noEntries(list: unknown[]): boolean {
  return list.length === 0;
}
