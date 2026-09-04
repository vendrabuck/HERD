import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Lab purpose classification, creation-pass preview (issue #646 phase 2, ADR
// 0013 point 8). Mirrors the debounced-hook test pattern in
// useForkAutosave.test.tsx: fake timers plus a mocked API call, no MSW.

const { classifyPurposePreviewMock } = vi.hoisted(() => ({
  classifyPurposePreviewMock: vi.fn(),
}));
vi.mock("@/api/ai", () => ({ classifyPurposePreview: classifyPurposePreviewMock }));

import { usePurposeSuggestion, type UsePurposeSuggestionOptions } from "@/hooks/usePurposeSuggestion";

const RESULT = {
  distribution: [
    { category: "qa_regression", probability: 0.7 },
    { category: "training", probability: 0.2 },
    { category: "other", probability: 0.1 },
  ],
  top_category: "qa_regression",
  pass: "creation" as const,
  model: "test-model",
  rationale: "matches regression keywords",
  generated_at: "2026-09-04T00:00:00Z",
  signals_used: ["purpose_text"],
};

function baseProps(overrides: Partial<UsePurposeSuggestionOptions> = {}): UsePurposeSuggestionOptions {
  return {
    enabled: true,
    categories: ["qa_regression", "training", "other"],
    purpose: "",
    topologyId: undefined,
    deviceIds: [],
    dynamicEntries: [],
    ...overrides,
  };
}

describe("usePurposeSuggestion", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    classifyPurposePreviewMock.mockReset();
    classifyPurposePreviewMock.mockResolvedValue(RESULT);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("makes no call when disabled, even with qualifying purpose text", async () => {
    renderHook((props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props), {
      initialProps: baseProps({ enabled: false, purpose: "long enough purpose text" }),
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(classifyPurposePreviewMock).not.toHaveBeenCalled();
  });

  it("makes no call when enabled but purpose is short and no topology is selected", async () => {
    renderHook((props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props), {
      initialProps: baseProps({ purpose: "short" }),
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(classifyPurposePreviewMock).not.toHaveBeenCalled();
  });

  it("calls after the debounce once purpose reaches 12 characters", async () => {
    const { result, rerender } = renderHook(
      (props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props),
      { initialProps: baseProps() },
    );
    rerender(baseProps({ purpose: "regression testing run" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(classifyPurposePreviewMock).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(classifyPurposePreviewMock).toHaveBeenCalledTimes(1);
    expect(result.current.suggestion).toEqual(RESULT);
  });

  it("triggers on a selected topology alone, regardless of purpose length", async () => {
    renderHook((props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props), {
      initialProps: baseProps({ topologyId: "topo-1" }),
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(classifyPurposePreviewMock).toHaveBeenCalledTimes(1);
    const [body] = classifyPurposePreviewMock.mock.calls[0];
    expect(body.topology_id).toBe("topo-1");
    expect(body.purpose).toBeNull();
  });

  it("resets the debounce on every keystroke and calls only once after typing stops", async () => {
    const { rerender } = renderHook(
      (props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props),
      { initialProps: baseProps() },
    );
    rerender(baseProps({ purpose: "regression te" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    rerender(baseProps({ purpose: "regression testing" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(classifyPurposePreviewMock).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(classifyPurposePreviewMock).toHaveBeenCalledTimes(1);
  });

  it("cancels a stale in-flight call: only the latest response is kept regardless of resolve order", async () => {
    let resolveFirst!: (v: typeof RESULT) => void;
    let resolveSecond!: (v: typeof RESULT) => void;
    classifyPurposePreviewMock
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve;
          }),
      );

    const { result, rerender } = renderHook(
      (props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props),
      { initialProps: baseProps() },
    );
    rerender(baseProps({ purpose: "regression testing one" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    rerender(baseProps({ purpose: "regression testing two changed" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });

    expect(classifyPurposePreviewMock).toHaveBeenCalledTimes(2);

    // The stale first call resolves after the second call was already made;
    // its (older) result must never overwrite the fresher one.
    await act(async () => {
      resolveFirst({ ...RESULT, top_category: "stale" });
      await Promise.resolve();
    });
    expect(result.current.suggestion?.top_category).not.toBe("stale");

    await act(async () => {
      resolveSecond({ ...RESULT, top_category: "fresh" });
      await Promise.resolve();
    });
    expect(result.current.suggestion?.top_category).toBe("fresh");
  });

  it("sets failed on a rejected call without throwing, and clears it on the next success", async () => {
    classifyPurposePreviewMock.mockRejectedValueOnce(new Error("network down"));
    const { result, rerender } = renderHook(
      (props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props),
      { initialProps: baseProps() },
    );
    rerender(baseProps({ purpose: "regression testing run" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(result.current.failed).toBe(true);
    expect(result.current.suggestion).toBeNull();

    rerender(baseProps({ purpose: "regression testing run retried" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(result.current.failed).toBe(false);
    expect(result.current.suggestion).toEqual(RESULT);
  });

  it("clears the suggestion once the trigger condition stops holding", async () => {
    const { result, rerender } = renderHook(
      (props: UsePurposeSuggestionOptions) => usePurposeSuggestion(props),
      { initialProps: baseProps({ purpose: "regression testing run" }) },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(result.current.suggestion).toEqual(RESULT);

    rerender(baseProps({ purpose: "" }));
    expect(result.current.suggestion).toBeNull();
  });
});
