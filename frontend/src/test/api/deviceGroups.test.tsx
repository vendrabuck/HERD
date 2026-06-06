import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useDeviceGroup,
  useDeviceGroups,
  useCreateDeviceGroup,
  useUpdateDeviceGroup,
  useDeleteDeviceGroup,
  useBulkAddDevices,
  useBulkRemoveDevices,
} from "@/api/deviceGroups";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const DG = { id: "dg1", name: "lab-A", description: null };

describe("deviceGroups api hooks", () => {
  it("useDeviceGroup is disabled without an id", () => {
    const { result } = renderHook(() => useDeviceGroup(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useDeviceGroups returns items", async () => {
    server.use(
      http.get("/api/inventory/device-groups", () =>
        HttpResponse.json({ items: [DG], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useDeviceGroups(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([DG]);
  });

  it("useCreateDeviceGroup POSTs the body", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/inventory/device-groups", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(DG);
      }),
    );
    const { result } = renderHook(() => useCreateDeviceGroup(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ name: "lab-A" });
    });
    expect((captured as { name: string }).name).toBe("lab-A");
  });

  it("useUpdateDeviceGroup PUTs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.put("/api/inventory/device-groups/dg1", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json(DG);
      }),
    );
    const { result } = renderHook(() => useUpdateDeviceGroup(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ id: "dg1", data: { name: "lab-B" } });
    });
    expect(capturedUrl).toMatch(/\/device-groups\/dg1$/);
  });

  it("useDeleteDeviceGroup DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/inventory/device-groups/dg1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteDeviceGroup(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("dg1");
    });
    expect(capturedUrl).toMatch(/\/device-groups\/dg1$/);
  });

  it("useBulkAddDevices POSTs device_ids", async () => {
    let captured: unknown = null;
    server.use(
      http.post(
        "/api/inventory/device-groups/dg1/devices/bulk",
        async ({ request }) => {
          captured = await request.json();
          return HttpResponse.json({ added: 2, skipped: 0 });
        },
      ),
    );
    const { result } = renderHook(() => useBulkAddDevices(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        groupId: "dg1",
        deviceIds: ["d1", "d2"],
      });
    });
    expect((captured as { device_ids: string[] }).device_ids).toEqual([
      "d1",
      "d2",
    ]);
  });

  it("useBulkRemoveDevices POSTs to the bulk-remove path", async () => {
    let capturedUrl = "";
    server.use(
      http.post(
        "/api/inventory/device-groups/dg1/devices/bulk-remove",
        ({ request }) => {
          capturedUrl = request.url;
          return HttpResponse.json({ removed: 1, not_found: 0 });
        },
      ),
    );
    const { result } = renderHook(() => useBulkRemoveDevices(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        groupId: "dg1",
        deviceIds: ["d1"],
      });
    });
    expect(capturedUrl).toMatch(/devices\/bulk-remove$/);
  });
});
