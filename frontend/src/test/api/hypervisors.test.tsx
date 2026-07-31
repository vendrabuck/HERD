import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useHypervisors,
  usePaginatedHypervisors,
  useCreateHypervisor,
  useUpdateHypervisor,
  useDeleteHypervisor,
} from "@/api/hypervisors";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const HV = {
  id: "h1",
  name: "Proxmox Lab",
  description: "lab cluster",
  endpoint: "https://proxmox.example.local:8006",
  hypervisor_type: "proxmox",
  secret_id: "s1",
  enabled: true,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  modified_by: null,
};

describe("hypervisors api hooks", () => {
  it("useHypervisors returns items", async () => {
    server.use(
      http.get("/api/inventory/hypervisors", () =>
        HttpResponse.json({ items: [HV], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useHypervisors(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([HV]);
  });

  it("usePaginatedHypervisors returns total", async () => {
    server.use(
      http.get("/api/inventory/hypervisors", () =>
        HttpResponse.json({ items: [HV], total: 4, skip: 0, limit: 50 }),
      ),
    );
    const { result } = renderHook(() => usePaginatedHypervisors(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBe(4);
  });

  it("useCreateHypervisor POSTs the body", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/inventory/hypervisors", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(HV, { status: 201 });
      }),
    );
    const { result } = renderHook(() => useCreateHypervisor(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        name: "Proxmox Lab",
        endpoint: "https://proxmox.example.local:8006",
        hypervisor_type: "proxmox",
        secret_id: "s1",
      });
    });
    expect((captured as { name: string }).name).toBe("Proxmox Lab");
  });

  it("useUpdateHypervisor PUTs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.put("/api/inventory/hypervisors/h1", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json(HV);
      }),
    );
    const { result } = renderHook(() => useUpdateHypervisor(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ id: "h1", data: { enabled: false } });
    });
    expect(capturedUrl).toMatch(/\/hypervisors\/h1$/);
  });

  it("useDeleteHypervisor DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/inventory/hypervisors/h1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteHypervisor(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("h1");
    });
    expect(capturedUrl).toMatch(/h1$/);
  });
});
