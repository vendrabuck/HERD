import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const { toastSuccess } = vi.hoisted(() => ({ toastSuccess: vi.fn() }));
vi.mock("react-hot-toast", () => ({ default: { success: toastSuccess, error: vi.fn() } }));

import { server } from "../mocks/server";
import { useForkVersionPreview } from "@/hooks/useForkVersionPreview";
import type { CanvasData } from "@/types/topology.types";
import type { ForkVersionSummary } from "@/types/reservation.types";

const RES_ID = "res-1";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function canvasWithNode(id: string, edges: CanvasData["edges"] = []): CanvasData {
  return {
    nodes: [
      {
        id,
        type: "deviceNode",
        position: { x: 0, y: 0 },
        data: { device: { id: `dev-${id}` }, label: id, topologyType: "PHYSICAL" },
      },
    ] as unknown as CanvasData["nodes"],
    edges,
    selectedEdgeLayer: "L1",
  };
}

const V1: ForkVersionSummary = {
  id: "v1",
  fork_id: "f1",
  version_number: 1,
  restored_from_id: null,
  created_at: "2026-08-28T00:00:00Z",
};
const V2: ForkVersionSummary = {
  id: "v2",
  fork_id: "f1",
  version_number: 2,
  restored_from_id: null,
  created_at: "2026-08-28T01:00:00Z",
};

function mockVersionDetail(version: ForkVersionSummary, canvas: CanvasData) {
  server.use(
    http.get(`/api/reservations/${RES_ID}/fork/versions/${version.id}`, () =>
      HttpResponse.json({ ...version, canvas_data: canvas }),
    ),
  );
}

describe("useForkVersionPreview", () => {
  it("starts idle and inactive", () => {
    const { result } = renderHook(
      () =>
        useForkVersionPreview({
          reservationId: RES_ID,
          currentCanvas: canvasWithNode("current"),
          loadCanvas: vi.fn(),
        }),
      { wrapper },
    );
    expect(result.current.mode).toBe("idle");
    expect(result.current.isActive).toBe(false);
  });

  it("preview loads the fetched version's canvas, ghosted as a proposal", async () => {
    mockVersionDetail(V1, canvasWithNode("n1"));
    const loadCanvas = vi.fn();
    const { result } = renderHook(
      () =>
        useForkVersionPreview({
          reservationId: RES_ID,
          currentCanvas: canvasWithNode("current"),
          loadCanvas,
        }),
      { wrapper },
    );

    result.current.startPreview(V1);
    await waitFor(() => expect(result.current.mode).toBe("preview"));
    await waitFor(() => expect(loadCanvas).toHaveBeenCalled());

    const loaded = loadCanvas.mock.calls[loadCanvas.mock.calls.length - 1][0] as CanvasData;
    expect(loaded.nodes[0].id).toBe("n1");
    expect((loaded.nodes[0].data as { isProposal?: boolean }).isProposal).toBe(true);
    expect(result.current.previewVersion).toEqual(V1);
  });

  it("exit restores the preserved live draft and resets to idle", async () => {
    const live = canvasWithNode("current");
    mockVersionDetail(V1, canvasWithNode("n1"));
    const loadCanvas = vi.fn();
    const { result } = renderHook(
      () => useForkVersionPreview({ reservationId: RES_ID, currentCanvas: live, loadCanvas }),
      { wrapper },
    );

    result.current.startPreview(V1);
    await waitFor(() => expect(loadCanvas).toHaveBeenCalled());
    loadCanvas.mockClear();

    result.current.exit();
    await waitFor(() => expect(result.current.mode).toBe("idle"));
    expect(loadCanvas).toHaveBeenCalledWith(live);
    expect(result.current.previewVersion).toBeNull();
  });

  it("diff against 'current' computes the diff from the captured live draft", async () => {
    const live = canvasWithNode("n1", [
      {
        id: "e1",
        source: "n1",
        target: "n1",
        type: "layerEdge",
        data: { layer: "L1", source_port_name: "eth1", target_port_name: "eth2" },
      } as unknown as CanvasData["edges"][number],
    ]);
    mockVersionDetail(V1, canvasWithNode("n1")); // no edges: v1 had none
    const loadCanvas = vi.fn();
    const { result } = renderHook(
      () => useForkVersionPreview({ reservationId: RES_ID, currentCanvas: live, loadCanvas }),
      { wrapper },
    );

    result.current.startDiff(V1, { kind: "current" });
    await waitFor(() => expect(result.current.diffResult).not.toBeNull());
    expect(result.current.diffResult!.addedEdges).toHaveLength(1);
    expect(result.current.diffCompareLabel).toBe("current draft");
  });

  it("diff between two versions computes added/removed independent of the live draft", async () => {
    mockVersionDetail(V1, canvasWithNode("n1"));
    mockVersionDetail(V2, canvasWithNode("n2"));
    const loadCanvas = vi.fn();
    const { result } = renderHook(
      () =>
        useForkVersionPreview({
          reservationId: RES_ID,
          currentCanvas: canvasWithNode("current"),
          loadCanvas,
        }),
      { wrapper },
    );

    result.current.startDiff(V1, { kind: "version", version: V2 });
    await waitFor(() => expect(result.current.diffResult).not.toBeNull());
    expect(result.current.diffResult!.addedNodes.map((n) => n.id)).toEqual(["n2"]);
    expect(result.current.diffResult!.removedNodes.map((n) => n.id)).toEqual(["n1"]);
    expect(result.current.diffCompareLabel).toBe("v2");
  });

  it("restoreVersion fetches the version's canvas, calls restore, then loads the restored canvas", async () => {
    const restoredCanvas = canvasWithNode("n1");
    mockVersionDetail(V1, restoredCanvas);
    let restoreCalled = false;
    server.use(
      http.post(`/api/reservations/${RES_ID}/fork/versions/${V1.id}/restore`, () => {
        restoreCalled = true;
        // Contract (revised 2026-08-28): restore appends no version; it only
        // echoes back which version was restored.
        return HttpResponse.json({
          id: "f1",
          valid: true,
          invalid_edges: [],
          draft_restored_from_id: V1.id,
        });
      }),
    );
    const loadCanvas = vi.fn();
    const { result } = renderHook(
      () =>
        useForkVersionPreview({
          reservationId: RES_ID,
          currentCanvas: canvasWithNode("current"),
          loadCanvas,
        }),
      { wrapper },
    );

    await result.current.restoreVersion(V1);

    expect(restoreCalled).toBe(true);
    expect(toastSuccess).toHaveBeenCalled();
    const loaded = loadCanvas.mock.calls[loadCanvas.mock.calls.length - 1][0] as CanvasData;
    expect(loaded.nodes[0].id).toBe("n1");
    await waitFor(() => expect(result.current.mode).toBe("idle"));
  });
});
