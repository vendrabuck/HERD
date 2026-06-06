import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  usePaginatedConnections,
  useDeviceConnections,
  useCreateConnection,
  useDeleteConnection,
  usePathfind,
  usePathfindPairs,
} from "@/api/connections";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const CONN = {
  id: "c1",
  source_port_id: "p1",
  target_port_id: "p2",
  source_device_id: "d1",
  target_device_id: "d2",
};

describe("connections api hooks", () => {
  it("usePaginatedConnections forwards skip, limit, device_id", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/cabling/connections", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ items: [CONN], total: 1, skip: 0, limit: 50 });
      }),
    );
    const { result } = renderHook(
      () => usePaginatedConnections(0, 50, "d1"),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(capturedUrl).toMatch(/device_id=d1/);
  });

  it("useDeviceConnections returns items only", async () => {
    server.use(
      http.get("/api/cabling/connections", () =>
        HttpResponse.json({ items: [CONN], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useDeviceConnections("d1"), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([CONN]);
  });

  it("useCreateConnection POSTs the body", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/cabling/connections", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(CONN);
      }),
    );
    const { result } = renderHook(() => useCreateConnection(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        device_a_id: "d1",
        port_a: "p1",
        device_b_id: "d2",
        port_b: "p2",
      });
    });
    expect((captured as { device_a_id: string }).device_a_id).toBe("d1");
  });

  it("useDeleteConnection DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/cabling/connections/c1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteConnection(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("c1");
    });
    expect(capturedUrl).toMatch(/c1$/);
  });

  it("usePathfind POSTs source and target ids", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/cabling/pathfind", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({ reachable: true, hop_count: 1, paths: [] });
      }),
    );
    const { result } = renderHook(() => usePathfind(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        sourceDeviceId: "d1",
        targetDeviceId: "d2",
      });
    });
    expect(captured).toEqual({
      source_device_id: "d1",
      target_device_id: "d2",
    });
  });

  it("usePathfindPairs aggregates results into a map keyed by pair", async () => {
    server.use(
      http.post("/api/cabling/pathfind", () =>
        HttpResponse.json({ reachable: true, hop_count: 2, paths: [] }),
      ),
    );
    const { result } = renderHook(
      () =>
        usePathfindPairs([
          { sourceDeviceId: "d1", targetDeviceId: "d2" },
        ]),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const map = result.current.data!;
    expect(map.get("d1::d2")?.reachable).toBe(true);
  });

  it("usePathfindPairs handles a per-pair error by recording unreachable", async () => {
    server.use(
      http.post("/api/cabling/pathfind", () =>
        HttpResponse.json({ detail: "no path" }, { status: 500 }),
      ),
    );
    const { result } = renderHook(
      () =>
        usePathfindPairs([
          { sourceDeviceId: "d1", targetDeviceId: "d2" },
        ]),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data!.get("d1::d2")?.reachable).toBe(false);
  });
});
