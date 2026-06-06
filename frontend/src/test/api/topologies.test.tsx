import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useTopology,
  useTopologies,
  usePaginatedTopologies,
  useCreateTopology,
  useUpdateTopology,
  useDeleteTopology,
} from "@/api/topologies";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const TOPO = {
  id: "t1",
  name: "spine-leaf",
  description: null,
  topology_type: "PHYSICAL",
  canvas_data: null,
  version_number: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("topologies api hooks", () => {
  it("useTopology is disabled without an id", () => {
    const { result } = renderHook(() => useTopology(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useTopologies returns items", async () => {
    server.use(
      http.get("/api/cabling/topologies", () =>
        HttpResponse.json({ items: [TOPO], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useTopologies(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([TOPO]);
  });

  it("usePaginatedTopologies returns total", async () => {
    server.use(
      http.get("/api/cabling/topologies", () =>
        HttpResponse.json({ items: [TOPO], total: 7, skip: 0, limit: 50 }),
      ),
    );
    const { result } = renderHook(() => usePaginatedTopologies(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBe(7);
  });

  it("useTopology fetches by id", async () => {
    server.use(
      http.get("/api/cabling/topologies/t1", () => HttpResponse.json(TOPO)),
    );
    const { result } = renderHook(() => useTopology("t1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.id).toBe("t1");
  });

  it("useCreateTopology POSTs the body", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/cabling/topologies", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(TOPO);
      }),
    );
    const { result } = renderHook(() => useCreateTopology(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        name: "spine-leaf",
      });
    });
    expect((captured as { name: string }).name).toBe("spine-leaf");
  });

  it("useUpdateTopology PUTs by id without the id in the body path arg", async () => {
    let captured: unknown = null;
    let capturedUrl = "";
    server.use(
      http.put("/api/cabling/topologies/t1", async ({ request }) => {
        capturedUrl = request.url;
        captured = await request.json();
        return HttpResponse.json(TOPO);
      }),
    );
    const { result } = renderHook(() => useUpdateTopology(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        id: "t1",
        description: "now with rationale",
      });
    });
    expect(capturedUrl).toMatch(/\/topologies\/t1$/);
    expect((captured as { id?: string }).id).toBeUndefined();
    expect((captured as { description: string }).description).toBe(
      "now with rationale",
    );
  });

  it("useDeleteTopology DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/cabling/topologies/t1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteTopology(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("t1");
    });
    expect(capturedUrl).toMatch(/\/topologies\/t1$/);
  });
});
