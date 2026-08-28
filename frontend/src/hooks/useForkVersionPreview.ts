import { useCallback, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { fetchForkVersion, forkVersionKey, useForkVersion, useRestoreForkVersion } from "@/api/reservations";
import type { ForkVersionSummary } from "@/types/reservation.types";
import type { CanvasData, DeviceNodeData, LayerEdgeData } from "@/types/topology.types";
import { buildForkDiffOverlayCanvas, diffForkCanvases, type ForkCanvasDiff } from "@/lib/forkDiff";
import { hydrateAndLoadCanvas } from "@/lib/canvasHydration";

export type ForkHistoryViewMode = "idle" | "preview" | "diff";

export type ForkDiffCompareTarget =
  | { kind: "version"; version: ForkVersionSummary }
  | { kind: "current" };

interface UseForkVersionPreviewParams {
  reservationId: string | null;
  // The live draft canvas (the editor's current nodes/edges), captured fresh
  // on every render. Used as the "current draft" side of a diff and as the
  // snapshot restored when the history view exits.
  currentCanvas: CanvasData | null;
  loadCanvas: (canvas: CanvasData) => void;
  // Fires the fork's debounced autosave immediately if a pending edit exists
  // (ForkAutosaveController.flush), called before this hook hijacks the
  // canvas store. Without this an edit made just before Preview/Diff sits
  // unsaved: the autosave effect's own cleanup only CANCELS a pending PUT
  // when it gets disabled (which entering a history view does, via
  // isReadOnly), it never sends one first.
  flushAutosave: () => void;
}

export interface UseForkVersionPreviewResult {
  mode: ForkHistoryViewMode;
  // True whenever a history view (preview or diff) has taken over the canvas:
  // callers lock editing, the wiring dialog, and Save on this.
  isActive: boolean;

  previewVersion: ForkVersionSummary | null;
  previewLoading: boolean;
  startPreview: (version: ForkVersionSummary) => void;

  diffBase: ForkVersionSummary | null;
  diffCompareLabel: string | null;
  diffLoading: boolean;
  diffResult: ForkCanvasDiff | null;
  startDiff: (base: ForkVersionSummary, compare: ForkDiffCompareTarget) => void;

  // Exits either preview or diff, restoring the live draft untouched.
  exit: () => void;

  restoreVersion: (version: ForkVersionSummary) => Promise<void>;
  isRestoring: boolean;
}

/**
 * Owns the fork history panel's preview/diff/restore canvas state (issue
 * #622), so TopologyEditorPage only wires this hook's result to its ReactFlow
 * props and the ForkHistoryPanel, rather than growing several more inline
 * useState/useCallback blocks.
 *
 * Preview and diff both work by temporarily loading a read-only canvas into
 * the shared editor store (the same technique the pre-existing parent-
 * topology version preview uses in TopologyEditorPage's handlePreviewVersion)
 * and restoring the preserved live draft on exit. Restore is different: it is
 * a real mutation (POST .../restore) that replaces the fork's DRAFT
 * canvas_data server-side; nothing is wired until the caller's own Save.
 */
export function useForkVersionPreview({
  reservationId,
  currentCanvas,
  loadCanvas,
  flushAutosave,
}: UseForkVersionPreviewParams): UseForkVersionPreviewResult {
  const [mode, setMode] = useState<ForkHistoryViewMode>("idle");
  const [previewVersion, setPreviewVersion] = useState<ForkVersionSummary | null>(null);
  const [diffBase, setDiffBase] = useState<ForkVersionSummary | null>(null);
  const [diffCompare, setDiffCompare] = useState<ForkDiffCompareTarget | null>(null);

  // The live draft as it stood the instant a history view was entered. Used
  // both to restore on exit and as the "current draft" side of a diff, since
  // by the time a diff resolves the store itself may already be painting a
  // locked, annotated canvas rather than the real draft. State, not a ref: it
  // is read during render (to build the diff), and a ref may not be read
  // there.
  const [preservedCanvas, setPreservedCanvas] = useState<CanvasData | null>(null);

  const queryClient = useQueryClient();
  const restoreMutation = useRestoreForkVersion(reservationId ?? "");

  const enterHistoryView = useCallback(() => {
    // Flush BEFORE the snapshot: latestRef inside useForkAutosave already
    // captures whatever is live right now, so ordering only matters in that
    // flush must run before loadCanvas below ever overwrites the store with
    // a preview/diff/restored canvas.
    flushAutosave();
    setPreservedCanvas((prev) => prev ?? currentCanvas ?? { nodes: [], edges: [] });
  }, [currentCanvas, flushAutosave]);

  const exit = useCallback(() => {
    if (preservedCanvas) {
      loadCanvas(preservedCanvas);
    }
    setPreservedCanvas(null);
    setMode("idle");
    setPreviewVersion(null);
    setDiffBase(null);
    setDiffCompare(null);
  }, [loadCanvas, preservedCanvas]);

  const startPreview = useCallback(
    (version: ForkVersionSummary) => {
      enterHistoryView();
      setDiffBase(null);
      setDiffCompare(null);
      setPreviewVersion(version);
      setMode("preview");
    },
    [enterHistoryView],
  );

  const startDiff = useCallback(
    (base: ForkVersionSummary, compare: ForkDiffCompareTarget) => {
      enterHistoryView();
      setPreviewVersion(null);
      setDiffBase(base);
      setDiffCompare(compare);
      setMode("diff");
    },
    [enterHistoryView],
  );

  const previewQuery = useForkVersion(reservationId, mode === "preview" ? previewVersion?.id : null);
  const diffBaseQuery = useForkVersion(reservationId, mode === "diff" ? diffBase?.id : null);
  const diffCompareVersionId =
    mode === "diff" && diffCompare?.kind === "version" ? diffCompare.version.id : null;
  const diffCompareQuery = useForkVersion(reservationId, diffCompareVersionId);

  const diffCompareCanvas: CanvasData | null =
    mode === "diff" && diffCompare?.kind === "current"
      ? preservedCanvas
      : (diffCompareQuery.data?.canvas_data ?? null);

  const diffReady =
    mode === "diff" &&
    !!diffBaseQuery.data &&
    (diffCompare?.kind === "current" || !!diffCompareQuery.data);

  const diffResult = useMemo<ForkCanvasDiff | null>(() => {
    if (!diffReady) return null;
    return diffForkCanvases(diffBaseQuery.data?.canvas_data ?? null, diffCompareCanvas);
  }, [diffReady, diffBaseQuery.data, diffCompareCanvas]);

  // Push the resolved preview/diff canvas onto the shared editor store once
  // it is ready. Preview ghosts the whole canvas (isProposal, matching the
  // parent-topology HistoryPanel's own preview treatment); diff shows the
  // compare side at full opacity with only the changed edges colored, since
  // the compare side can be the live draft itself, which should not look
  // like an AI ghost proposal. Both route through hydrateAndLoadCanvas
  // (issue #622 review), not a raw loadCanvas: a version's canvas_data comes
  // straight off the server and can carry thin nodes (`{ device: { id } }`
  // with no name/topology_type), exactly like the normal fork/topology load
  // path this mirrors.
  useEffect(() => {
    let cancelled = false;
    if (mode === "preview" && previewQuery.data?.canvas_data) {
      const canvas = previewQuery.data.canvas_data;
      const ghosted: CanvasData = {
        ...canvas,
        nodes: canvas.nodes.map((n) => ({
          ...n,
          data: { ...(n.data as DeviceNodeData), isProposal: true },
        })),
        edges: canvas.edges.map((e) => ({
          ...e,
          data: { ...((e.data as LayerEdgeData | undefined) ?? { layer: "L1" }), isProposal: true },
        })),
      };
      void hydrateAndLoadCanvas(ghosted, (hydrated) => {
        if (!cancelled) loadCanvas(hydrated);
      });
    } else if (mode === "diff" && diffResult && diffCompareCanvas) {
      const overlay = buildForkDiffOverlayCanvas(diffCompareCanvas, diffResult);
      void hydrateAndLoadCanvas(overlay, (hydrated) => {
        if (!cancelled) loadCanvas(hydrated);
      });
    }
    return () => {
      cancelled = true;
    };
  }, [mode, previewQuery.data, diffResult, diffCompareCanvas, loadCanvas]);

  const restoreVersion = useCallback(
    async (version: ForkVersionSummary) => {
      if (!reservationId) return;
      // The restore response carries no canvas payload (ForkVersionRestoreResult
      // is ForkCanvasUpdateResponse-shaped); the version's own canvas_data is
      // immutable, so fetching (or reusing the cached) version detail gives the
      // exact payload the restore just copied onto the draft.
      const detail = await queryClient.fetchQuery({
        queryKey: forkVersionKey(reservationId, version.id),
        queryFn: () => fetchForkVersion(reservationId, version.id),
      });
      await restoreMutation.mutateAsync(version.id);
      exit();
      if (detail.canvas_data) {
        await hydrateAndLoadCanvas(detail.canvas_data, loadCanvas);
      }
    },
    [reservationId, queryClient, restoreMutation, exit, loadCanvas],
  );

  return {
    mode,
    isActive: mode !== "idle",
    previewVersion,
    previewLoading: mode === "preview" && previewQuery.isLoading,
    startPreview,
    diffBase,
    diffCompareLabel:
      mode !== "diff" || !diffCompare
        ? null
        : diffCompare.kind === "current"
          ? "current draft"
          : `v${diffCompare.version.version_number}`,
    diffLoading:
      mode === "diff" &&
      (diffBaseQuery.isLoading || (diffCompare?.kind === "version" && diffCompareQuery.isLoading)),
    diffResult,
    startDiff,
    exit,
    restoreVersion,
    isRestoring: restoreMutation.isPending,
  };
}
