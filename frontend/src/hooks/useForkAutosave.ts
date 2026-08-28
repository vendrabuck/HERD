import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { putForkCanvas } from "@/api/reservations";
import type { CanvasData } from "@/types/topology.types";

// The debounced fork-draft autosave interval. A draft PUT is cheap by design
// (loose store, no reconcile, no version append), so we save a couple of seconds
// after edits pause rather than on every keystroke-equivalent canvas mutation.
export const FORK_AUTOSAVE_DELAY_MS = 2000;

export type ForkAutosaveStatus = "idle" | "saving" | "saved" | "error";

export interface ForkAutosaveController {
  status: ForkAutosaveStatus;
  // Mark the current canvas as already persisted (call after an explicit save)
  // so the unmount flush does not re-PUT a draft the reconcile already captured.
  markClean: () => void;
  // Fire the pending debounced PUT immediately (and cancel its timer) if the
  // canvas has diverged from the last-saved baseline; a no-op otherwise. Call
  // this before anything hijacks the canvas out from under the debounce (a
  // fork-history preview/diff, issue #622 review): the debounce effect's own
  // cleanup only CANCELS a pending PUT when `enabled` flips false, it never
  // sends one, so an edit made just before entering a history view would
  // otherwise sit unsaved until the next real edit re-arms the timer.
  flush: () => void;
}

// A content-only signature of the canvas: the fields that define the wiring,
// stripped of React Flow's transient per-render churn (selection, drag flags,
// measured dimensions). Two canvases with the same signature are the same draft,
// so pure selection/hover activity never triggers a spurious draft PUT.
function canvasSignature(canvas: CanvasData): string {
  const nodes = (canvas.nodes ?? []).map((n) => ({
    id: n.id,
    type: n.type,
    position: n.position,
    data: n.data,
  }));
  const edges = (canvas.edges ?? []).map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle ?? null,
    targetHandle: e.targetHandle ?? null,
    type: e.type,
    data: e.data,
  }));
  return JSON.stringify({ nodes, edges, selectedEdgeLayer: canvas.selectedEdgeLayer });
}

/**
 * Debounced autosave of a reservation fork's draft canvas (ADR 0006 Decision 6).
 *
 * When enabled, PUTs the canvas to the loose-draft endpoint FORK_AUTOSAVE_DELAY_MS
 * after the last edit, and flushes any unsaved draft on unmount (navigate-away).
 * The freshly loaded canvas is seeded as the baseline the first time autosave is
 * enabled, so loading a fork never counts as an edit.
 */
export function useForkAutosave(params: {
  reservationId: string | null;
  canvas: CanvasData;
  enabled: boolean;
  delay?: number;
}): ForkAutosaveController {
  const { reservationId, canvas, enabled, delay = FORK_AUTOSAVE_DELAY_MS } = params;
  const [status, setStatus] = useState<ForkAutosaveStatus>("idle");

  const signature = useMemo(() => canvasSignature(canvas), [canvas]);

  // The last signature we have persisted (or seeded as the baseline). null means
  // "no baseline yet": autosave stays inert until it is seeded on first enable.
  const lastSavedRef = useRef<string | null>(null);
  // Latest values, mirrored into a ref so the unmount cleanup (a stable closure)
  // can flush the current draft rather than a stale one. Synced in an effect
  // (never during render) so it reflects each committed render.
  const latestRef = useRef<{ id: string | null; signature: string; canvas: CanvasData }>({
    id: reservationId,
    signature,
    canvas,
  });
  useEffect(() => {
    latestRef.current = { id: reservationId, signature, canvas };
  }, [reservationId, signature, canvas]);

  // Seed the baseline when autosave becomes enabled, and clear it when disabled
  // (read-only, or leaving live-edit) so a later re-enable re-seeds cleanly.
  const enabledRef = useRef(false);
  useEffect(() => {
    if (enabled && !enabledRef.current) {
      enabledRef.current = true;
      lastSavedRef.current = signature;
      setStatus("idle");
    } else if (!enabled && enabledRef.current) {
      enabledRef.current = false;
      lastSavedRef.current = null;
    }
  }, [enabled, signature]);

  // The pending debounce timer's handle, mirrored outside the effect so flush()
  // (an imperative call, not a render-time effect) can cancel it directly.
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounced draft PUT. Every canvas change reschedules the timer; it fires only
  // after `delay` ms of quiet, PUTting the latest canvas.
  useEffect(() => {
    if (!enabled || !reservationId) return;
    if (lastSavedRef.current === null) return; // baseline not seeded yet
    if (signature === lastSavedRef.current) return; // no unsaved change

    const handle = setTimeout(() => {
      timeoutRef.current = null;
      const target = latestRef.current;
      if (!target.id) return;
      setStatus("saving");
      putForkCanvas(target.id, target.canvas)
        .then(() => {
          lastSavedRef.current = target.signature;
          setStatus("saved");
        })
        .catch(() => setStatus("error"));
    }, delay);
    timeoutRef.current = handle;

    return () => {
      clearTimeout(handle);
      if (timeoutRef.current === handle) timeoutRef.current = null;
    };
  }, [signature, enabled, reservationId, delay]);

  // Flush an unsaved draft on unmount (navigate-away). Fire-and-forget: cleanup
  // cannot await, but the loose PUT is idempotent and never appends a version.
  useEffect(() => {
    return () => {
      const target = latestRef.current;
      if (!target.id) return;
      if (lastSavedRef.current === null) return;
      if (target.signature === lastSavedRef.current) return;
      void putForkCanvas(target.id, target.canvas).catch(() => undefined);
      lastSavedRef.current = target.signature;
    };
  }, []);

  const markClean = () => {
    lastSavedRef.current = latestRef.current.signature;
    setStatus("saved");
  };

  const flush = useCallback(() => {
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    const target = latestRef.current;
    if (!target.id) return;
    if (lastSavedRef.current === null) return; // baseline not seeded; nothing to flush
    if (target.signature === lastSavedRef.current) return; // no unsaved change

    setStatus("saving");
    putForkCanvas(target.id, target.canvas)
      .then(() => {
        lastSavedRef.current = target.signature;
        setStatus("saved");
      })
      .catch(() => setStatus("error"));
  }, []);

  return { status, markClean, flush };
}
