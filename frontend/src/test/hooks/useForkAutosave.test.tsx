import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CanvasData } from "@/types/topology.types";

const { putForkCanvasMock } = vi.hoisted(() => ({ putForkCanvasMock: vi.fn() }));
vi.mock("@/api/reservations", () => ({ putForkCanvas: putForkCanvasMock }));

import { useForkAutosave } from "@/hooks/useForkAutosave";

const RES_ID = "res-1";

function canvasWith(label: string): CanvasData {
  return {
    nodes: [
      {
        id: "n-1",
        type: "deviceNode",
        position: { x: 0, y: 0 },
        data: { device: { id: "d-1" }, label, topologyType: "PHYSICAL" },
      },
    ] as unknown as CanvasData["nodes"],
    edges: [],
    selectedEdgeLayer: "L2",
  };
}

describe("useForkAutosave", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    putForkCanvasMock.mockReset();
    putForkCanvasMock.mockResolvedValue({ id: "f-1", valid: true, invalid_edges: [] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not autosave the freshly loaded canvas (baseline is not an edit)", async () => {
    renderHook(
      (props: { canvas: CanvasData }) =>
        useForkAutosave({ reservationId: RES_ID, canvas: props.canvas, enabled: true, delay: 2000 }),
      { initialProps: { canvas: canvasWith("a") } },
    );
    await vi.advanceTimersByTimeAsync(3000);
    expect(putForkCanvasMock).not.toHaveBeenCalled();
  });

  it("PUTs the draft after the debounce interval elapses following an edit", async () => {
    const { rerender } = renderHook(
      (props: { canvas: CanvasData }) =>
        useForkAutosave({ reservationId: RES_ID, canvas: props.canvas, enabled: true, delay: 2000 }),
      { initialProps: { canvas: canvasWith("a") } },
    );

    rerender({ canvas: canvasWith("edited") });
    // Before the interval elapses, nothing is sent.
    await vi.advanceTimersByTimeAsync(1000);
    expect(putForkCanvasMock).not.toHaveBeenCalled();
    // After it elapses, the latest canvas is PUT once.
    await vi.advanceTimersByTimeAsync(1000);
    expect(putForkCanvasMock).toHaveBeenCalledTimes(1);
    expect(putForkCanvasMock).toHaveBeenCalledWith(RES_ID, canvasWith("edited"));
  });

  it("debounces rapid edits into a single PUT of the final canvas", async () => {
    const { rerender } = renderHook(
      (props: { canvas: CanvasData }) =>
        useForkAutosave({ reservationId: RES_ID, canvas: props.canvas, enabled: true, delay: 2000 }),
      { initialProps: { canvas: canvasWith("a") } },
    );

    rerender({ canvas: canvasWith("b") });
    await vi.advanceTimersByTimeAsync(1500);
    rerender({ canvas: canvasWith("c") });
    await vi.advanceTimersByTimeAsync(1500);
    // The first edit's timer was reset by the second edit: no PUT yet.
    expect(putForkCanvasMock).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(600);
    expect(putForkCanvasMock).toHaveBeenCalledTimes(1);
    expect(putForkCanvasMock).toHaveBeenCalledWith(RES_ID, canvasWith("c"));
  });

  it("flushes an unsaved draft on unmount (navigate-away)", async () => {
    const { rerender, unmount } = renderHook(
      (props: { canvas: CanvasData }) =>
        useForkAutosave({ reservationId: RES_ID, canvas: props.canvas, enabled: true, delay: 2000 }),
      { initialProps: { canvas: canvasWith("a") } },
    );

    rerender({ canvas: canvasWith("dirty") });
    // Unmount before the debounce timer fires; the flush must still send it.
    unmount();
    expect(putForkCanvasMock).toHaveBeenCalledTimes(1);
    expect(putForkCanvasMock).toHaveBeenCalledWith(RES_ID, canvasWith("dirty"));
  });

  it("does not flush on unmount when there is no unsaved edit", () => {
    const { unmount } = renderHook(() =>
      useForkAutosave({ reservationId: RES_ID, canvas: canvasWith("a"), enabled: true, delay: 2000 }),
    );
    unmount();
    expect(putForkCanvasMock).not.toHaveBeenCalled();
  });

  it("stays inert when disabled (read-only fork)", async () => {
    const { rerender } = renderHook(
      (props: { canvas: CanvasData }) =>
        useForkAutosave({ reservationId: RES_ID, canvas: props.canvas, enabled: false, delay: 2000 }),
      { initialProps: { canvas: canvasWith("a") } },
    );
    rerender({ canvas: canvasWith("edited") });
    await vi.advanceTimersByTimeAsync(3000);
    expect(putForkCanvasMock).not.toHaveBeenCalled();
  });
});
