import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useDevice,
  useDevices,
  usePaginatedDevices,
  useAllDeviceNames,
} from "@/api/inventory";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const device = (overrides: Record<string, unknown> = {}) => ({
  id: "dev-1",
  name: "dev-1",
  template_id: "tpl-1",
  template_name: "Alpha",
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
  field_data: {},
  ...overrides,
});

describe("inventory api hooks", () => {
  it("useDevice is disabled without an id", () => {
    const { result } = renderHook(() => useDevice(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useDevice fetches by id", async () => {
    server.use(
      http.get("/api/inventory/devices/dev-1", () => HttpResponse.json(device())),
    );
    const { result } = renderHook(() => useDevice("dev-1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.id).toBe("dev-1");
  });

  it("useDevices forwards filter query params", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/inventory/devices", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 });
      }),
    );
    const { result } = renderHook(
      () => useDevices({ template_id: "tpl-1", status: "AVAILABLE", dut_only: true, search: "foo" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(capturedUrl).toMatch(/template_id=tpl-1/);
    expect(capturedUrl).toMatch(/status=AVAILABLE/);
    expect(capturedUrl).toMatch(/dut_only=true/);
    expect(capturedUrl).toMatch(/search=foo/);
  });

  it("usePaginatedDevices returns the raw paginated shape", async () => {
    server.use(
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({ items: [device()], total: 1, skip: 0, limit: 25 }),
      ),
    );
    const { result } = renderHook(() => usePaginatedDevices(undefined, 0, 25), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBe(1);
    expect(result.current.data?.items).toHaveLength(1);
  });

  it("useAllDeviceNames stops paging on a short page", async () => {
    let calls = 0;
    server.use(
      http.get("/api/inventory/devices", () => {
        calls += 1;
        // First page is short so paging halts after one fetch.
        return HttpResponse.json({
          items: [device({ id: "a", name: "aa" }), device({ id: "b", name: "bb" })],
          total: 2,
          skip: 0,
          limit: 500,
        });
      }),
    );
    const { result } = renderHook(() => useAllDeviceNames(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(calls).toBe(1);
    expect(result.current.data?.get("a")).toBe("aa");
    expect(result.current.data?.get("b")).toBe("bb");
  });
});
