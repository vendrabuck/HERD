import { describe, it, expect, vi } from "vitest";

import {
  fetchAllPages,
  LIST_MAX_PAGES,
  LIST_PAGE_SIZE_MAX,
  ListPagingLimitError,
} from "@/api/paging";
import type { PaginatedResponse } from "@/types/pagination.types";

// A list of `count` rows served in pages, as a HERD list route serves them:
// `limit` capped at the route's maximum, `total` the whole count.
function serverOf(count: number) {
  const rows = Array.from({ length: count }, (_, i) => i);
  const calls: Array<[number, number]> = [];
  const fetchPage = vi.fn(async (skip: number, limit: number): Promise<PaginatedResponse<number>> => {
    calls.push([skip, limit]);
    return { items: rows.slice(skip, skip + limit), total: count, skip, limit };
  });
  return { rows, calls, fetchPage };
}

describe("fetchAllPages (issues #703 and #1336)", () => {
  it("asks for the route maximum, 500 rows a page, and stops at 200 pages", () => {
    expect(LIST_PAGE_SIZE_MAX).toBe(500);
    expect(LIST_MAX_PAGES).toBe(200);
  });

  // The boundary space around one page: every count from empty to two full
  // pages plus one, with the exact request sequence each must cost.
  it.each([
    [0, [[0, 500]]],
    [1, [[0, 500]]],
    [499, [[0, 500]]],
    [500, [[0, 500]]],
    [501, [[0, 500], [500, 500]]],
    [999, [[0, 500], [500, 500]]],
    [1000, [[0, 500], [500, 500]]],
    [1001, [[0, 500], [500, 500], [1000, 500]]],
  ])("%i rows come back whole, in order, from exactly the pages needed", async (count, expected) => {
    const { rows, calls, fetchPage } = serverOf(count);
    await expect(fetchAllPages(fetchPage)).resolves.toEqual(rows);
    expect(calls).toEqual(expected);
  });

  it("a total that overstates the rows ends on the first empty page", async () => {
    // Rows deleted mid-walk leave total ahead of the data: the empty page ends
    // the walk instead of requesting up to the stale total.
    const calls: number[] = [];
    const fetchPage = async (skip: number, limit: number) => {
      calls.push(skip);
      const items = skip < 1000 ? Array.from({ length: limit }, (_, i) => skip + i) : [];
      return { items, total: 1500, skip, limit };
    };
    const all = await fetchAllPages(fetchPage);
    expect(calls).toEqual([0, 500, 1000]);
    expect(all).toHaveLength(1000);
  });

  it("a short page ends the walk even when total claims more", async () => {
    const calls: number[] = [];
    const fetchPage = async (skip: number, limit: number) => {
      calls.push(skip);
      const items = skip === 0 ? Array.from({ length: limit }, (_, i) => i) : [500, 501];
      return { items, total: 5000, skip, limit };
    };
    const all = await fetchAllPages(fetchPage);
    expect(calls).toEqual([0, 500]);
    expect(all).toHaveLength(502);
  });

  it("a failed page mid-walk rejects the whole walk with that error, never a partial list", async () => {
    const boom = new Error("page 2 failed");
    const calls: number[] = [];
    const fetchPage = async (skip: number, limit: number) => {
      calls.push(skip);
      if (skip === 500) throw boom;
      return { items: Array.from({ length: limit }, (_, i) => i), total: 1200, skip, limit };
    };
    await expect(fetchAllPages(fetchPage)).rejects.toBe(boom);
    expect(calls).toEqual([0, 500]);
  });

  it("a server that never ends is cut off at maxPages with ListPagingLimitError", async () => {
    let calls = 0;
    const page = Array.from({ length: 500 }, (_, i) => i);
    const fetchPage = async (skip: number, limit: number) => {
      calls += 1;
      // Full pages forever, and a total that keeps growing ahead of skip.
      return { items: page, total: skip + 2 * limit, skip, limit };
    };
    const err = await fetchAllPages(fetchPage).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ListPagingLimitError);
    expect((err as Error).message).toBe("The list did not end within 200 pages");
    expect(calls).toBe(LIST_MAX_PAGES);
  });

  it("a list that ends on exactly the last allowed page resolves", async () => {
    const { rows, calls, fetchPage } = serverOf(4 * 3);
    await expect(fetchAllPages(fetchPage, { pageSize: 3, maxPages: 4 })).resolves.toEqual(rows);
    expect(calls).toHaveLength(4);
  });

  it("one row past the last allowed page rejects instead of dropping it", async () => {
    const { fetchPage } = serverOf(4 * 3 + 1);
    await expect(fetchAllPages(fetchPage, { pageSize: 3, maxPages: 4 })).rejects.toBeInstanceOf(
      ListPagingLimitError,
    );
  });
});
