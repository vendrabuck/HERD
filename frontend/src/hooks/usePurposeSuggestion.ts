import { useEffect, useRef, useState } from "react";

import { classifyPurposePreview } from "@/api/ai";
import type { PurposeClassification } from "@/types/reservation.types";

// The creation-pass preview debounce (issue #646 phase 2, ADR 0013 point 8).
export const PURPOSE_SUGGESTION_DEBOUNCE_MS = 700;
// The trigger condition on the purpose text alone; a selected topology
// triggers regardless of text length (see `shouldTrigger` below).
const MIN_PURPOSE_CHARS = 12;

export interface DynamicRequestCount {
  templateId: string;
  count: number;
}

export interface UsePurposeSuggestionOptions {
  // Gate: useAIStatus().purpose_classification. False means "render nothing,
  // call nothing" for the whole feature, not just a disabled control.
  enabled: boolean;
  categories: string[];
  purpose: string;
  topologyId: string | undefined;
  deviceIds: string[];
  dynamicEntries: DynamicRequestCount[];
}

export interface UsePurposeSuggestionResult {
  // The most recently resolved suggestion; kept across a new in-flight
  // request so the UI does not flicker blank while a fresher call runs.
  suggestion: PurposeClassification | null;
  // True once a call has failed; cleared by the next successful call. The
  // caller renders the muted "Suggestion unavailable" text on this alone,
  // never a raw error.
  failed: boolean;
}

// Debounced, cancel-stale, one-in-flight preview call for the create modal.
// Never throws and never exposes loading/error states beyond `failed`: a
// classification suggestion is advisory only and must never block or even
// visibly disturb reservation creation.
export function usePurposeSuggestion({
  enabled,
  categories,
  purpose,
  topologyId,
  deviceIds,
  dynamicEntries,
}: UsePurposeSuggestionOptions): UsePurposeSuggestionResult {
  const [suggestion, setSuggestion] = useState<PurposeClassification | null>(null);
  const [failed, setFailed] = useState(false);
  const requestIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const trimmedPurpose = purpose.trim();
  const shouldTrigger = enabled && (trimmedPurpose.length >= MIN_PURPOSE_CHARS || !!topologyId);

  // Stable, order-independent signatures so the effect only re-fires on an
  // actual content change, not a new array/object identity from the caller's
  // own re-render.
  const deviceIdsKey = [...deviceIds].sort().join(",");
  const dynamicKey = dynamicEntries
    .map((e) => `${e.templateId}:${e.count}`)
    .sort()
    .join(",");
  const categoriesKey = categories.join(",");

  useEffect(() => {
    if (!shouldTrigger) {
      // Feature off, or the trigger condition no longer holds (e.g. the user
      // cleared the purpose text on a device-only reservation): abandon any
      // in-flight call and drop the last suggestion, since it no longer
      // corresponds to the current inputs.
      abortRef.current?.abort();
      // Intentional state sync: the trigger condition itself just changed,
      // so the last suggestion is stale by definition the instant this runs.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSuggestion(null);
      setFailed(false);
      return;
    }

    const timer = setTimeout(() => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const requestId = ++requestIdRef.current;

      const dynamicRequests = dynamicEntries.length
        ? dynamicEntries.map((e) => ({ template_id: e.templateId, count: e.count }))
        : null;

      classifyPurposePreview(
        {
          categories,
          purpose: trimmedPurpose || null,
          topology_id: topologyId ?? null,
          device_ids: deviceIds.length ? deviceIds : null,
          dynamic_requests: dynamicRequests,
        },
        controller.signal,
      )
        .then((result) => {
          if (requestIdRef.current !== requestId) return; // superseded by a newer call
          setSuggestion(result);
          setFailed(false);
        })
        .catch(() => {
          if (controller.signal.aborted) return; // superseded; not a real failure
          if (requestIdRef.current !== requestId) return;
          setFailed(true);
        });
    }, PURPOSE_SUGGESTION_DEBOUNCE_MS);

    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldTrigger, trimmedPurpose, topologyId, deviceIdsKey, dynamicKey, categoriesKey]);

  return { suggestion, failed };
}
