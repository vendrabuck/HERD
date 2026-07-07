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
  hydrateCanvasNodes,
} from "@/api/inventory";
import type { CanvasData, DeviceNodeData } from "@/types/topology.types";

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

// Build a canvas with thin nodes ({ device: { id } }), mirroring an old seeded
// or stale persisted topology, to exercise the load-time hydration.
function canvasWith(nodeDevices: Array<Record<string, unknown> | undefined>): CanvasData {
  return {
    nodes: nodeDevices.map((d, i) => ({
      id: `n${i}`,
      type: "deviceNode",
      position: { x: 0, y: 0 },
      data: (d ? { device: d } : {}) as unknown as DeviceNodeData,
    })),
    edges: [],
  } as CanvasData;
}

// Mock the batch endpoint: returns the given devices as {"items": [...]} and
// records each request's device_ids body so tests can pin call count and payload.
function mockBatch(devices: Array<Record<string, unknown>>, capturedBodies: string[][] = []) {
  server.use(
    http.post("/api/inventory/devices/batch", async ({ request }) => {
      const body = (await request.json()) as { device_ids: string[] };
      capturedBodies.push(body.device_ids);
      return HttpResponse.json({ items: devices });
    }),
  );
  return capturedBodies;
}

describe("hydrateCanvasNodes", () => {
  it("fills thin nodes with the fetched name, topology_type, and label", async () => {
    mockBatch([device({ id: "dev-1", name: "L1-Edge-01", topology_type: "CLOUD" })]);

    const hydrated = await hydrateCanvasNodes(canvasWith([{ id: "dev-1" }]));

    const data = hydrated.nodes[0].data as DeviceNodeData;
    expect(data.device.name).toBe("L1-Edge-01");
    expect(data.device.topology_type).toBe("CLOUD");
    // label/topologyType are refreshed so the node reads correctly on the canvas.
    expect(data.label).toBe("L1-Edge-01");
    expect(data.topologyType).toBe("CLOUD");
  });

  it("issues one batch POST for all nodes, with repeated ids deduped", async () => {
    const bodies = mockBatch([
      device({ id: "dev-1", name: "Shared" }),
      device({ id: "dev-2", name: "Other" }),
    ]);

    const hydrated = await hydrateCanvasNodes(
      canvasWith([{ id: "dev-1" }, { id: "dev-1" }, { id: "dev-2" }]),
    );

    // One request total; its body carries each unique id exactly once.
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toEqual(["dev-1", "dev-2"]);
    expect((hydrated.nodes[0].data as DeviceNodeData).device.name).toBe("Shared");
    expect((hydrated.nodes[1].data as DeviceNodeData).device.name).toBe("Shared");
    expect((hydrated.nodes[2].data as DeviceNodeData).device.name).toBe("Other");
  });

  it("keeps a node whose device is omitted from the batch (deleted or not visible)", async () => {
    // Server omits "gone" entirely; the whole batch still succeeds.
    mockBatch([]);

    const original = canvasWith([{ id: "gone", name: "stale-name" }]);
    const hydrated = await hydrateCanvasNodes(original);

    // Node is retained with its persisted (stale) data, not dropped or blanked.
    expect(hydrated.nodes).toHaveLength(1);
    const data = hydrated.nodes[0].data as DeviceNodeData;
    expect((data.device as unknown as Record<string, unknown>).id).toBe("gone");
    expect((data.device as unknown as Record<string, unknown>).name).toBe("stale-name");
  });

  it("leaves empty (missing-device) nodes untouched and does not fetch", async () => {
    const bodies = mockBatch([device()]);

    const hydrated = await hydrateCanvasNodes(canvasWith([undefined]));

    expect(bodies).toHaveLength(0);
    expect(hydrated.nodes[0].data).toEqual({});
  });

  it("hydrates surviving nodes even when a sibling device is omitted", async () => {
    mockBatch([device({ id: "ok", name: "Alive" })]);

    const hydrated = await hydrateCanvasNodes(canvasWith([{ id: "ok" }, { id: "gone" }]));

    expect((hydrated.nodes[0].data as DeviceNodeData).device.name).toBe("Alive");
    // The omitted node keeps its thin reference (id only), still present.
    expect((hydrated.nodes[1].data as DeviceNodeData).device.name).toBeUndefined();
  });

  it("keeps every node's existing data when the batch request itself fails", async () => {
    server.use(
      http.post("/api/inventory/devices/batch", () => new HttpResponse(null, { status: 500 })),
    );

    const hydrated = await hydrateCanvasNodes(canvasWith([{ id: "dev-1", name: "stale" }]));

    // Never throws; the node survives with its persisted data and renders as
    // a DeviceNode.
    expect(hydrated.nodes).toHaveLength(1);
    const data = hydrated.nodes[0].data as DeviceNodeData;
    expect((data.device as unknown as Record<string, unknown>).name).toBe("stale");
    expect(hydrated.nodes[0].type).toBe("deviceNode");
  });

  it("sets type 'deviceNode' on a typeless device node so it renders, not as a blank box", async () => {
    mockBatch([device({ id: "dev-1", name: "Repaired" })]);

    // A topology persisted before the seed fix: device-backed node with type null.
    const canvas = {
      nodes: [
        {
          id: "n0",
          type: null,
          position: { x: 0, y: 0 },
          data: { device: { id: "dev-1" } } as unknown as DeviceNodeData,
        },
      ],
      edges: [],
    } as unknown as CanvasData;

    const hydrated = await hydrateCanvasNodes(canvas);

    expect(hydrated.nodes[0].type).toBe("deviceNode");
    expect((hydrated.nodes[0].data as DeviceNodeData).device.name).toBe("Repaired");
  });

  it("sets type 'deviceNode' even when the device is omitted (typeless stale node)", async () => {
    mockBatch([]);

    const canvas = {
      nodes: [
        {
          id: "n0",
          type: null,
          position: { x: 0, y: 0 },
          data: { device: { id: "gone" } } as unknown as DeviceNodeData,
        },
      ],
      edges: [],
    } as unknown as CanvasData;

    const hydrated = await hydrateCanvasNodes(canvas);

    // Even with no fresh data, the node must render through DeviceNode, not the
    // blank default renderer, so the discriminator is set regardless.
    expect(hydrated.nodes[0].type).toBe("deviceNode");
  });
});
