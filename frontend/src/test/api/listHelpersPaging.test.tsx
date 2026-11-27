import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, type UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useAllUsers } from "@/api/admin";
import { useGroups } from "@/api/groups";
import { useDeviceGroups } from "@/api/deviceGroups";
import { useHypervisors } from "@/api/hypervisors";
import { useDrivers } from "@/api/drivers";
import { useTemplates } from "@/api/templates";
import { useDeviceConnections } from "@/api/connections";
import { useAllDevices } from "@/api/inventory";
import { ListPagingLimitError } from "@/api/paging";

// Issue #1336: every "all of them" list helper walks the server's pages
// instead of asking for page 0 of the 500-row cap and dropping `total`. Each
// helper is driven through its real hook at the MSW seam the #703 walker
// tests use, so the request it sends (path, skip, limit, and its filter on
// every page) is what is checked, not a mock of the walker.

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

interface HelperCase {
  name: string;
  path: string;
  // The filter the helper must send on every page, if it has one.
  filter?: [string, string];
  useHelper: () => UseQueryResult<unknown[]>;
}

const HELPERS: HelperCase[] = [
  { name: "useAllUsers", path: "/api/auth/users", useHelper: () => useAllUsers() },
  { name: "useGroups", path: "/api/auth/groups", useHelper: () => useGroups() },
  { name: "useDeviceGroups", path: "/api/inventory/device-groups", useHelper: () => useDeviceGroups() },
  { name: "useHypervisors", path: "/api/inventory/hypervisors", useHelper: () => useHypervisors() },
  { name: "useDrivers", path: "/api/inventory/drivers", useHelper: () => useDrivers() },
  {
    name: "useTemplates",
    path: "/api/inventory/templates",
    filter: ["template_type", "device"],
    useHelper: () => useTemplates("device"),
  },
  {
    name: "useDeviceConnections",
    path: "/api/cabling/connections",
    filter: ["device_id", "dev-7"],
    useHelper: () => useDeviceConnections("dev-7"),
  },
  {
    name: "useAllDevices",
    path: "/api/inventory/devices",
    filter: ["status", "AVAILABLE"],
    useHelper: () => useAllDevices({ status: "AVAILABLE" }),
  },
];

interface Seen {
  skip: string | null;
  limit: string | null;
  filter: string | null;
}

// Serve `count` rows in pages the way a HERD list route does. `failAtSkip`
// answers 500 for that page instead.
function serve(c: HelperCase, count: number, failAtSkip?: number): Seen[] {
  const seen: Seen[] = [];
  server.use(
    http.get(c.path, ({ request }) => {
      const url = new URL(request.url);
      const skip = Number(url.searchParams.get("skip"));
      const limit = Number(url.searchParams.get("limit"));
      seen.push({
        skip: url.searchParams.get("skip"),
        limit: url.searchParams.get("limit"),
        filter: c.filter ? url.searchParams.get(c.filter[0]) : null,
      });
      if (skip === failAtSkip) return HttpResponse.json({ detail: "boom" }, { status: 500 });
      const end = Math.min(count, skip + limit);
      const items = Array.from({ length: Math.max(0, end - skip) }, (_, i) => ({ id: `row-${skip + i}` }));
      return HttpResponse.json({ items, total: count, skip, limit });
    }),
  );
  return seen;
}

describe.each(HELPERS)("$name pages past the 500-row cap", (c) => {
  const filterValue = c.filter ? c.filter[1] : null;
  const page = (skip: string): Seen => ({ skip, limit: "500", filter: filterValue });

  it("returns all 501 rows of a two-page list, from skip=0 then skip=500", async () => {
    const seen = serve(c, 501);
    const { result } = renderHook(() => c.useHelper(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seen).toEqual([page("0"), page("500")]);
    expect(result.current.data).toHaveLength(501);
    expect(result.current.data?.[500]).toEqual({ id: "row-500" });
  });

  it("exactly one full page costs one request and returns all 500", async () => {
    const seen = serve(c, 500);
    const { result } = renderHook(() => c.useHelper(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seen).toEqual([page("0")]);
    expect(result.current.data).toHaveLength(500);
  });

  it("an empty list costs one request and returns []", async () => {
    const seen = serve(c, 0);
    const { result } = renderHook(() => c.useHelper(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seen).toEqual([page("0")]);
    expect(result.current.data).toEqual([]);
  });

  it("a failed second page is an error with no data, never the first 500 rows", async () => {
    const seen = serve(c, 1200, 500);
    const { result } = renderHook(() => c.useHelper(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(seen).toEqual([page("0"), page("500")]);
    expect(result.current.data).toBeUndefined();
  });

  it("a list that never ends stops at 200 requests with an error", async () => {
    let requests = 0;
    const full = Array.from({ length: 500 }, (_, i) => ({ id: `row-${i}` }));
    server.use(
      http.get(c.path, ({ request }) => {
        requests += 1;
        const skip = Number(new URL(request.url).searchParams.get("skip"));
        return HttpResponse.json({ items: full, total: skip + 1000, skip, limit: 500 });
      }),
    );
    const { result } = renderHook(() => c.useHelper(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 20_000 });
    expect(result.current.error).toBeInstanceOf(ListPagingLimitError);
    expect(result.current.data).toBeUndefined();
    expect(requests).toBe(200);
  }, 30_000);
});
