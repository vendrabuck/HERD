import type { PaginatedResponse } from "@/types/pagination.types";

// The largest `limit` every HERD list endpoint accepts (`le=500` on each
// route's Query; one more answers 422). A walker asks for this much per page
// so a collection that fits in one page costs exactly one request.
export const LIST_PAGE_SIZE_MAX = 500;

// The most pages one walk will request: 200 pages of 500 is 100,000 rows.
// Past it the walk rejects instead of returning what it has, so a server that
// never ends (a `total` that keeps growing, or full pages forever) cannot hang
// the page, and no caller is handed a silently partial list.
export const LIST_MAX_PAGES = 200;

/** A page walk that reached LIST_MAX_PAGES before the server said the list ended. */
export class ListPagingLimitError extends Error {
  constructor(maxPages: number) {
    super(`The list did not end within ${maxPages} pages`);
    this.name = "ListPagingLimitError";
  }
}

/**
 * Walk every page of a `{items, total, skip, limit}` list endpoint and return
 * the rows of all of them, concatenated in order (issues #703 and #1336), so a
 * list that must be complete (a picker, an index, a lookup) is not cut at the
 * server's page cap. `fetchPage(skip, limit)` fetches one page with the
 * caller's filters.
 *
 * The result is the whole row set, each row once, only when the server's
 * order is a stable total order and no row is inserted or deleted during the
 * walk. Every route this helper walks has a total order since #1589, so only a
 * list that changes during the walk can still repeat a row or skip one at a
 * page boundary; this helper neither detects nor removes that.
 *
 * The walk ends on a short page (fewer rows than asked for, an empty page
 * included) or when `skip` reaches the reported `total`. Pages are fetched
 * one after another, so a list of N rows costs ceil(N / pageSize) requests
 * (one, for an empty list or exactly one full page that `total` closes).
 * Any failed page rejects the whole walk: a partial list is exactly the
 * defect this helper exists to prevent, so the caller sees an error state.
 * Reaching `maxPages` without an end rejects with ListPagingLimitError.
 */
export async function fetchAllPages<T>(
  fetchPage: (skip: number, limit: number) => Promise<PaginatedResponse<T>>,
  options?: { pageSize?: number; maxPages?: number },
): Promise<T[]> {
  const pageSize = options?.pageSize ?? LIST_PAGE_SIZE_MAX;
  const maxPages = options?.maxPages ?? LIST_MAX_PAGES;
  const all: T[] = [];
  let skip = 0;
  for (let page = 0; page < maxPages; page++) {
    const resp = await fetchPage(skip, pageSize);
    all.push(...resp.items);
    skip += resp.items.length;
    if (resp.items.length < pageSize) return all;
    if (skip >= resp.total) return all;
  }
  throw new ListPagingLimitError(maxPages);
}
