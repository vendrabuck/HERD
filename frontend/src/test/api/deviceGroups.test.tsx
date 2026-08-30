import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useDeviceGroup,
  useDeviceGroups,
  usePaginatedDeviceGroups,
  useDeviceGroupsForDevice,
  useCreateDeviceGroup,
  useUpdateDeviceGroup,
  useDeleteDeviceGroup,
  useBulkAddDevices,
  useBulkRemoveDevices,
  useBulkAddUserGroups,
  useBulkRemoveUserGroups,
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

  it("useBulkAddUserGroups POSTs user_group_ids to the permissions bulk path", async () => {
    let captured: unknown = null;
    let capturedUrl = "";
    server.use(
      http.post(
        "/api/inventory/device-groups/dg1/permissions/bulk",
        async ({ request }) => {
          captured = await request.json();
          capturedUrl = request.url;
          return HttpResponse.json({ added: 1, skipped: 0 });
        },
      ),
    );
    const { result } = renderHook(() => useBulkAddUserGroups(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ groupId: "dg1", userGroupIds: ["ug1"] });
    });
    expect((captured as { user_group_ids: string[] }).user_group_ids).toEqual(["ug1"]);
    expect(capturedUrl).toMatch(/permissions\/bulk$/);
  });

  it("useBulkRemoveUserGroups POSTs to the permissions bulk-remove path", async () => {
    let capturedUrl = "";
    server.use(
      http.post(
        "/api/inventory/device-groups/dg1/permissions/bulk-remove",
        ({ request }) => {
          capturedUrl = request.url;
          return HttpResponse.json({ removed: 1, not_found: 0 });
        },
      ),
    );
    const { result } = renderHook(() => useBulkRemoveUserGroups(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ groupId: "dg1", userGroupIds: ["ug1"] });
    });
    expect(capturedUrl).toMatch(/permissions\/bulk-remove$/);
  });

  it("useDeviceGroupsForDevice is disabled without a device id", () => {
    const { result } = renderHook(() => useDeviceGroupsForDevice(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useDeviceGroupsForDevice fetches memberships for a device id", async () => {
    const membership = [{ device_group_id: "dg1", device_group_name: "lab-A" }];
    server.use(
      http.get("/api/inventory/device-groups/device/d1", () =>
        HttpResponse.json(membership),
      ),
    );
    const { result } = renderHook(() => useDeviceGroupsForDevice("d1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(membership);
  });

  it("usePaginatedDeviceGroups passes skip and limit as query params", async () => {
    let capturedParams: URLSearchParams | undefined;
    server.use(
      http.get("/api/inventory/device-groups", ({ request }) => {
        capturedParams = new URL(request.url).searchParams;
        return HttpResponse.json({ items: [DG], total: 1, skip: 10, limit: 20 });
      }),
    );
    const { result } = renderHook(() => usePaginatedDeviceGroups(10, 20), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(capturedParams?.get("skip")).toBe("10");
    expect(capturedParams?.get("limit")).toBe("20");
  });

  it("useDeviceGroup fetches by id when one is provided", async () => {
    const detail = { ...DG, devices: [], user_groups: [] };
    server.use(
      http.get("/api/inventory/device-groups/dg1", () => HttpResponse.json(detail)),
    );
    const { result } = renderHook(() => useDeviceGroup("dg1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(detail);
  });
});
