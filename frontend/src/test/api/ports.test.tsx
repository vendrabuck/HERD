import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  usePorts,
  useCreatePort,
  useUpdatePort,
  useCreatePortsBulk,
  useDeletePort,
} from "@/api/ports";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const PORT = {
  id: "p1",
  device_id: "d1",
  name: "eth0",
  port_type: "ethernet",
  speed_gbps: 10,
  is_cabled: false,
};

describe("ports api hooks", () => {
  it("usePorts is disabled without a device id", () => {
    const { result } = renderHook(() => usePorts(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("usePorts fetches the device's port list", async () => {
    server.use(
      http.get("/api/inventory/devices/d1/ports", () =>
        HttpResponse.json([PORT]),
      ),
    );
    const { result } = renderHook(() => usePorts("d1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toHaveLength(1);
  });

  it("useCreatePort POSTs the body", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/inventory/devices/d1/ports", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(PORT);
      }),
    );
    const { result } = renderHook(() => useCreatePort(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        deviceId: "d1",
        data: { name: "eth0", template_id: "pt1", field_data: {} },
      });
    });
    expect((captured as { name: string }).name).toBe("eth0");
  });

  it("useUpdatePort PUTs to /inventory/ports/{portId}", async () => {
    let capturedUrl = "";
    server.use(
      http.put("/api/inventory/ports/p1", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json(PORT);
      }),
    );
    const { result } = renderHook(() => useUpdatePort(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        portId: "p1",
        data: { name: "eth1" },
      });
    });
    expect(capturedUrl).toMatch(/\/api\/inventory\/ports\/p1$/);
  });

  it("useDeletePort DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/inventory/ports/p1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeletePort(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("p1");
    });
    expect(capturedUrl).toMatch(/\/api\/inventory\/ports\/p1$/);
  });

  it("useCreatePortsBulk POSTs the bulk body to the device's ports/bulk endpoint", async () => {
    let capturedUrl = "";
    let capturedBody: unknown = null;
    server.use(
      http.post("/api/inventory/devices/d1/ports/bulk", async ({ request }) => {
        capturedUrl = request.url;
        capturedBody = await request.json();
        return HttpResponse.json([PORT, { ...PORT, id: "p2", name: "eth1" }]);
      }),
    );
    const { result } = renderHook(() => useCreatePortsBulk(), { wrapper });
    let created: unknown;
    await act(async () => {
      created = await result.current.mutateAsync({
        deviceId: "d1",
        data: { name_prefix: "eth", starting_index: 0, instances: 2, template_id: "pt1", field_data: {} },
      });
    });
    expect(capturedUrl).toMatch(/\/devices\/d1\/ports\/bulk$/);
    expect(capturedBody).toEqual({
      name_prefix: "eth",
      starting_index: 0,
      instances: 2,
      template_id: "pt1",
      field_data: {},
    });
    expect(created).toHaveLength(2);
  });
});
