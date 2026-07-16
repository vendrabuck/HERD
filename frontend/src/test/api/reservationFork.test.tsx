import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useReservationFork,
  useSaveReservationFork,
  putForkCanvas,
  saveReservationFork,
  forkConflictDetail,
} from "@/api/reservations";
import type { CanvasData } from "@/types/topology.types";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const RES_ID = "11111111-1111-1111-1111-111111111111";

const FORK = {
  id: "f0000000-0000-0000-0000-000000000001",
  reservation_id: RES_ID,
  parent_topology_id: "topo-1",
  parent_version_id: null,
  status: "ACTIVE",
  canvas_data: { nodes: [], edges: [], selectedEdgeLayer: "L2" },
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
  connections: [],
  versions: [
    {
      id: "v-1",
      fork_id: "f0000000-0000-0000-0000-000000000001",
      version_number: 1,
      restored_from_id: null,
      created_at: "2026-06-01T00:00:00Z",
    },
  ],
};

const EMPTY_CANVAS: CanvasData = { nodes: [], edges: [], selectedEdgeLayer: "L2" };

describe("reservation fork api", () => {
  it("useReservationFork GETs /reservations/{id}/fork and returns the fork", async () => {
    server.use(http.get(`/api/reservations/${RES_ID}/fork`, () => HttpResponse.json(FORK)));
    const { result } = renderHook(() => useReservationFork(RES_ID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.status).toBe("ACTIVE");
    expect(result.current.data?.versions).toHaveLength(1);
  });

  it("useReservationFork stays disabled with no reservation id", () => {
    const { result } = renderHook(() => useReservationFork(null), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("putForkCanvas PUTs the loose draft to the canvas endpoint", async () => {
    let method = "";
    let body: unknown;
    server.use(
      http.put(`/api/reservations/${RES_ID}/fork/canvas`, async ({ request }) => {
        method = request.method;
        body = await request.json();
        return HttpResponse.json({ id: FORK.id, valid: true, invalid_edges: [] });
      }),
    );
    const result = await putForkCanvas(RES_ID, EMPTY_CANVAS);
    expect(method).toBe("PUT");
    expect(body).toEqual({ canvas_data: EMPTY_CANVAS });
    expect(result.valid).toBe(true);
  });

  it("saveReservationFork POSTs to the save endpoint and returns the delta", async () => {
    server.use(
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({
          fork_id: FORK.id,
          version_number: 2,
          released: [
            { device_a_id: "d-a", port_a: "1", device_b_id: "d-b", port_b: "2", layer: "L2" },
          ],
          built: [
            { device_a_id: "d-a", port_a: "1", device_b_id: "d-c", port_b: "3", layer: "L2" },
          ],
          unchanged_count: 4,
        }),
      ),
    );
    const result = await saveReservationFork(RES_ID, EMPTY_CANVAS);
    expect(result.version_number).toBe(2);
    expect(result.released).toHaveLength(1);
    expect(result.built).toHaveLength(1);
    expect(result.unchanged_count).toBe(4);
  });

  it("useSaveReservationFork mutation resolves with the save result", async () => {
    server.use(
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({
          fork_id: FORK.id,
          version_number: 3,
          released: [],
          built: [],
          unchanged_count: 0,
        }),
      ),
    );
    const { result } = renderHook(() => useSaveReservationFork(), { wrapper });
    result.current.mutate({ reservationId: RES_ID, canvasData: EMPTY_CANVAS });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.version_number).toBe(3);
  });
});

describe("forkConflictDetail", () => {
  it("extracts a structured port-claim 409 detail", () => {
    const err = {
      response: {
        status: 409,
        data: {
          detail: {
            message: "One or more ports are already claimed by another active reservation",
            conflicts: [{ reservation_id: "r-2", device_id: "d-9", port: "eth1" }],
          },
        },
      },
    };
    const detail = forkConflictDetail(err);
    expect(detail).not.toBeNull();
    expect(detail?.conflicts[0]).toEqual({
      reservation_id: "r-2",
      device_id: "d-9",
      port: "eth1",
    });
  });

  it("returns null for a plain-string 409 (non-active / archived refusal)", () => {
    const err = {
      response: { status: 409, data: { detail: "Fork save requires an ACTIVE reservation" } },
    };
    expect(forkConflictDetail(err)).toBeNull();
  });

  it("returns null for a non-409 error", () => {
    expect(forkConflictDetail({ response: { status: 503, data: { detail: "down" } } })).toBeNull();
    expect(forkConflictDetail(new Error("boom"))).toBeNull();
  });
});
