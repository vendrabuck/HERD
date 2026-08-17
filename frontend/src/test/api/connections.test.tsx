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
  useCreateConnectionsBulk,
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

  it("useCreateConnectionsBulk POSTs the items under an items key", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/cabling/connections/bulk", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({
          created: 1,
          rejected: 0,
          rows: [{ index: 0, status: "created", connection_id: "c1", error: null }],
        });
      }),
    );
    const { result } = renderHook(() => useCreateConnectionsBulk(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync([
        { device_a_id: "d1", port_a: "p1", device_b_id: "d2", port_b: "p2" },
      ]);
    });
    expect(captured).toEqual({
      items: [{ device_a_id: "d1", port_a: "p1", device_b_id: "d2", port_b: "p2" }],
    });
  });

  it("useCreateConnectionsBulk resolves (not rejects) a 200 carrying rejected rows", async () => {
    // Row-level rejections ride inside a 200 body. A caller that only watched
    // the promise settling would report unqualified success here.
    server.use(
      http.post("/api/cabling/connections/bulk", () =>
        HttpResponse.json({
          created: 1,
          rejected: 1,
          rows: [
            { index: 0, status: "created", connection_id: "c1", error: null },
            { index: 1, status: "rejected", connection_id: null, error: "Port not found" },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useCreateConnectionsBulk(), { wrapper });
    let response;
    await act(async () => {
      response = await result.current.mutateAsync([
        { device_a_id: "d1", port_a: "p1", device_b_id: "d2", port_b: "p2" },
        { device_a_id: "d1", port_a: "p3", device_b_id: "d2", port_b: "p4" },
      ]);
    });
    expect(response).toMatchObject({ created: 1, rejected: 1 });
    expect(result.current.isSuccess).toBe(true);
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

  it("usePathfindPairs issues ONE batch request and maps results by pair", async () => {
    let requestCount = 0;
    let capturedBody: unknown = null;
    server.use(
      http.post("/api/cabling/pathfind/batch", async ({ request }) => {
        requestCount += 1;
        const body = (await request.json()) as {
          pairs: { source_device_id: string; target_device_id: string }[];
        };
        capturedBody = body;
        return HttpResponse.json({
          results: body.pairs.map((p, idx) => ({
            source_device_id: p.source_device_id,
            target_device_id: p.target_device_id,
            reachable: idx === 0,
            hop_count: idx === 0 ? 2 : 0,
            paths: [],
          })),
        });
      }),
    );
    const { result } = renderHook(
      () =>
        usePathfindPairs([
          { sourceDeviceId: "d1", targetDeviceId: "d2" },
          { sourceDeviceId: "d1", targetDeviceId: "d3" },
          { sourceDeviceId: "d2", targetDeviceId: "d3" },
        ]),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // Three pairs, exactly one HTTP request.
    expect(requestCount).toBe(1);
    expect(
      (capturedBody as { pairs: unknown[] }).pairs,
    ).toHaveLength(3);
    const map = result.current.data!;
    expect(map.get("d1::d2")?.reachable).toBe(true);
    expect(map.get("d1::d2")?.hop_count).toBe(2);
    expect(map.get("d1::d3")?.reachable).toBe(false);
    expect(map.get("d2::d3")?.reachable).toBe(false);
  });

  it("usePathfindPairs splits pair lists beyond the batch cap into chunks", async () => {
    const chunkSizes: number[] = [];
    server.use(
      http.post("/api/cabling/pathfind/batch", async ({ request }) => {
        const body = (await request.json()) as {
          pairs: { source_device_id: string; target_device_id: string }[];
        };
        chunkSizes.push(body.pairs.length);
        return HttpResponse.json({
          results: body.pairs.map((p) => ({
            source_device_id: p.source_device_id,
            target_device_id: p.target_device_id,
            reachable: true,
            hop_count: 2,
            paths: [],
          })),
        });
      }),
    );
    const pairs = Array.from({ length: 2001 }, (_, i) => ({
      sourceDeviceId: "src",
      targetDeviceId: `tgt-${i}`,
    }));
    const { result } = renderHook(() => usePathfindPairs(pairs), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // 2001 pairs against a 2000-pair server cap: two requests, all results kept.
    expect(chunkSizes).toEqual([2000, 1]);
    expect(result.current.data!.size).toBe(2001);
    expect(result.current.data!.get("src::tgt-2000")?.reachable).toBe(true);
  });

  it("usePathfindPairs surfaces a batch request failure as a query error", async () => {
    server.use(
      http.post("/api/cabling/pathfind/batch", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    const { result } = renderHook(
      () =>
        usePathfindPairs([
          { sourceDeviceId: "d1", targetDeviceId: "d2" },
        ]),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });
});
