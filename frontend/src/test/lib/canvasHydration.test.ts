import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CanvasData } from "@/types/topology.types";

const { hydrateCanvasNodesMock } = vi.hoisted(() => ({
  hydrateCanvasNodesMock: vi.fn(),
}));
vi.mock("@/api/inventory", () => ({ hydrateCanvasNodes: hydrateCanvasNodesMock }));

import { hydrateAndLoadCanvas } from "@/lib/canvasHydration";

const canvas: CanvasData = { nodes: [], edges: [] };

describe("hydrateAndLoadCanvas", () => {
  beforeEach(() => {
    hydrateCanvasNodesMock.mockReset();
  });

  it("loads the hydrated canvas returned by hydrateCanvasNodes, not the original", async () => {
    const hydrated: CanvasData = {
      nodes: [
        {
          id: "n0",
          type: "deviceNode",
          position: { x: 0, y: 0 },
          data: { device: { id: "dev-1" }, label: "dev-1", topologyType: "PHYSICAL" },
        },
      ] as unknown as CanvasData["nodes"],
      edges: [],
    };
    hydrateCanvasNodesMock.mockResolvedValue(hydrated);
    const loadCanvas = vi.fn();

    await hydrateAndLoadCanvas(canvas, loadCanvas);

    expect(hydrateCanvasNodesMock).toHaveBeenCalledWith(canvas);
    expect(loadCanvas).toHaveBeenCalledTimes(1);
    expect(loadCanvas).toHaveBeenCalledWith(hydrated);
  });

  it("falls back to loading the original canvas when hydrateCanvasNodes rejects", async () => {
    hydrateCanvasNodesMock.mockRejectedValue(new Error("boom"));
    const loadCanvas = vi.fn();

    await hydrateAndLoadCanvas(canvas, loadCanvas);

    expect(loadCanvas).toHaveBeenCalledTimes(1);
    expect(loadCanvas).toHaveBeenCalledWith(canvas);
  });

  it("never throws even when hydration rejects", async () => {
    hydrateCanvasNodesMock.mockRejectedValue(new Error("network down"));

    await expect(hydrateAndLoadCanvas(canvas, vi.fn())).resolves.toBeUndefined();
  });
});
