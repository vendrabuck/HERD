import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";

// Lab purpose classification, creation-pass preview and prefill (issue #646
// phase 2, ADR 0013 point 8). A sibling file to CreateReservationModal.test.tsx,
// isolated so the AI-status and preview-endpoint fixtures do not have to
// thread through every existing case in that file.

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

import { server } from "../mocks/server";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const DEVICE_IDS = ["d-1", "d-2"];

function mockAIStatus(purposeClassification: boolean) {
  server.use(
    http.get("/api/ai/status", () =>
      HttpResponse.json({ enabled: true, purpose_classification: purposeClassification }),
    ),
  );
}

function mockPurposeCategories(categories: string[] = ["qa_regression", "training", "other"]) {
  server.use(
    http.get("/api/reservations/purpose-categories", () => HttpResponse.json({ categories })),
  );
}

const PREVIEW_RESULT = {
  distribution: [
    { category: "qa_regression", probability: 0.72 },
    { category: "training", probability: 0.18 },
    { category: "other", probability: 0.1 },
  ],
  top_category: "qa_regression",
  pass: "creation",
  model: "test-model",
  rationale: "mentions regression suite",
  generated_at: "2026-09-04T00:00:00Z",
  signals_used: ["purpose_text"],
};

function mockPreview(handler?: () => Response | Promise<Response>, calls?: { count: number }) {
  server.use(
    http.post("/api/ai/classify-purpose/preview", async () => {
      if (calls) calls.count += 1;
      if (handler) return handler();
      return HttpResponse.json(PREVIEW_RESULT);
    }),
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  mockPurposeCategories();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("CreateReservationModal purpose suggestion (issue #646 phase 2)", () => {
  it("renders nothing and calls nothing when purpose_classification is off", async () => {
    mockAIStatus(false);
    const calls = { count: 0 };
    mockPreview(undefined, calls);

    renderWithProviders(<CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText("Purpose (optional)"), {
      target: { value: "regression testing run" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });

    expect(calls.count).toBe(0);
    expect(screen.queryByText(/Suggested:/)).not.toBeInTheDocument();
    expect(screen.queryByText("Suggestion unavailable")).not.toBeInTheDocument();
  });

  it("calls the preview after the debounce once purpose reaches 12 characters, and shows the suggestion", async () => {
    mockAIStatus(true);
    mockPreview();

    renderWithProviders(<CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText("Purpose (optional)"), {
      target: { value: "regression testing run" },
    });

    // Before the debounce elapses, nothing shows.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(screen.queryByText(/Suggested:/)).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    await waitFor(() =>
      expect(screen.getByText(/Suggested: QA and regression 72%/)).toBeInTheDocument(),
    );
    // Top three categories rendered as chips.
    expect(screen.getByText("Training 18%")).toBeInTheDocument();
    expect(screen.getByText("Other 10%")).toBeInTheDocument();
  });

  it("prefills the still-Unclassified select from the suggestion", async () => {
    mockAIStatus(true);
    mockPreview();

    renderWithProviders(<CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />);
    const select = screen.getByLabelText("Purpose category (optional)") as HTMLSelectElement;
    expect(select.value).toBe("");

    fireEvent.change(screen.getByLabelText("Purpose (optional)"), {
      target: { value: "regression testing run" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });

    await waitFor(() => expect(select.value).toBe("qa_regression"));
  });

  it("stops prefilling once the user changes the select manually", async () => {
    mockAIStatus(true);
    mockPreview();

    renderWithProviders(<CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />);
    const select = screen.getByLabelText("Purpose category (optional)") as HTMLSelectElement;
    const purposeInput = screen.getByLabelText("Purpose (optional)");

    fireEvent.change(purposeInput, { target: { value: "regression testing run" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    await waitFor(() => expect(select.value).toBe("qa_regression"));

    // The user overrides the suggestion by hand.
    fireEvent.change(select, { target: { value: "training" } });
    expect(select.value).toBe("training");

    // A later, different suggestion must not overwrite the manual choice.
    mockPreview(() =>
      HttpResponse.json({ ...PREVIEW_RESULT, top_category: "other" }),
    );
    fireEvent.change(purposeInput, {
      target: { value: "regression testing run, more detail" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });

    expect(select.value).toBe("training");
  });

  it("shows a muted Suggestion unavailable message on a failed preview call and never blocks submit", async () => {
    mockAIStatus(true);
    mockPreview(() => HttpResponse.json({ detail: "boom" }, { status: 502 }));

    renderWithProviders(<CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText("Purpose (optional)"), {
      target: { value: "regression testing run" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });

    await waitFor(() => expect(screen.getByText("Suggestion unavailable")).toBeInTheDocument());
    expect(screen.queryByText(/Suggested:/)).not.toBeInTheDocument();
    // Submit stays enabled: a device is selected, so hasResources is true and
    // the suggestion failure never touches the submit gate.
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });
});
