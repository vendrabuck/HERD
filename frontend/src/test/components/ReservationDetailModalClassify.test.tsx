import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Admin "Classify now" action in the reservation detail modal (issue #822).
// Real hooks and msw, so the request, the in-flight state, and the query
// invalidation are exercised end to end; only toasts and heavy tabs are stubbed.

// The Modal uses a native <dialog>. The global setup (src/test/setup.ts) stubs
// showModal so it sets `open`, which keeps the contents in the accessibility
// tree for getByRole.

const { toastFn, toastSuccess, toastError } = vi.hoisted(() => {
  const toastSuccess = vi.fn();
  const toastError = vi.fn();
  const toastFn = Object.assign(vi.fn(), { success: toastSuccess, error: toastError });
  return { toastFn, toastSuccess, toastError };
});
vi.mock("react-hot-toast", () => ({ default: toastFn }));

let currentRole = "admin";
vi.mock("@/stores/authStore", () => {
  const state = () => ({
    user: { id: "admin-1", role: currentRole, username: "admin", email: "a@b.c" },
    accessToken: null,
    refreshToken: null,
    setTokens: () => {},
    clearAuth: () => {},
  });
  const useAuthStore = (selector: (s: ReturnType<typeof state>) => unknown) => selector(state());
  useAuthStore.getState = state;
  return { useAuthStore };
});
vi.mock("@/api/ai", () => ({ useAIStatus: () => ({ data: { enabled: false } }) }));
vi.mock("@/components/reservations/ReservationInventoryTab", () => ({
  ReservationInventoryTab: () => <div />,
}));
vi.mock("@/components/reservations/ReservationRoutesTab", () => ({
  ReservationRoutesTab: () => <div />,
}));
vi.mock("@/components/reservations/ReservationStatusTab", () => ({
  ReservationStatusTab: () => <div />,
}));
vi.mock("@/components/reservations/AIAssistantTab", () => ({ AIAssistantTab: () => <div /> }));
vi.mock("@/components/reservations/AIApplyConfirmModal", () => ({
  AIApplyConfirmModal: () => <div />,
}));
vi.mock("@/components/reservations/EditDevicesModal", () => ({
  EditDevicesModal: () => <div />,
}));

import { server } from "../mocks/server";
import { ReservationDetailModal } from "@/components/reservations/ReservationDetailModal";
import {
  PURPOSE_CLASSIFY_ALREADY_SUGGESTED_MESSAGE,
  PURPOSE_CLASSIFY_DISABLED_MESSAGE,
  PURPOSE_CLASSIFY_NOT_ELIGIBLE_MESSAGE,
  PURPOSE_CLASSIFY_OUTCOME_MESSAGES,
} from "@/lib/purposeClassify";
import type { PurposeClassification, Reservation } from "@/types/reservation.types";

const ID = "res-9";
const URL = `/api/reservations/admin/purpose-review/${ID}/classify`;

const BASE: Reservation = {
  id: ID,
  user_id: "someone-else",
  owner_name: "alice",
  device_ids: [],
  topology_id: null,
  topology_type: "PHYSICAL",
  purpose: "regression",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "CANCELLED",
  created_at: "2026-05-01T00:00:00Z",
};

const SUGGESTION: PurposeClassification = {
  distribution: [{ category: "qa_regression", probability: 0.9 }],
  top_category: "qa_regression",
  pass: "end",
  model: "m",
  rationale: "r",
  generated_at: "2026-06-02T01:00:00Z",
  signals_used: ["purpose_text"],
};

function renderModal(overrides: Partial<Reservation> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ReservationDetailModal
          reservation={{ ...BASE, ...overrides }}
          deviceNames={new Map()}
          onClose={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { invalidate };
}

const button = () => screen.getByRole("button", { name: /classify now|classifying/i });

beforeEach(() => {
  currentRole = "admin";
  toastFn.mockReset();
  toastSuccess.mockReset();
  toastError.mockReset();
});

describe("ReservationDetailModal Classify now", () => {
  it.each(["COMPLETED", "CANCELLED", "FAILED"] as const)(
    "is visible for an admin on a %s reservation without a suggestion",
    (status) => {
      renderModal({ status });
      expect(button()).toBeEnabled();
    },
  );

  it("is absent for a non-admin", () => {
    currentRole = "user";
    renderModal();
    expect(screen.queryByRole("button", { name: /classify now/i })).not.toBeInTheDocument();
  });

  it.each(["PENDING", "PENDING_PROVISION", "ACTIVE"] as const)(
    "is absent while the reservation is %s",
    (status) => {
      renderModal({ status });
      expect(screen.queryByRole("button", { name: /classify now/i })).not.toBeInTheDocument();
    },
  );

  it("is absent once a suggestion exists", () => {
    renderModal({ purpose_suggestion: SUGGESTION });
    expect(screen.queryByRole("button", { name: /classify now/i })).not.toBeInTheDocument();
  });

  it("on ok: names the category, hides the button, and invalidates the reservation queries", async () => {
    server.use(
      http.post(URL, () =>
        HttpResponse.json({ reservation_id: ID, outcome: "ok", purpose_suggestion: SUGGESTION }),
      ),
    );
    const { invalidate } = renderModal();
    fireEvent.click(button());
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1));
    expect(toastSuccess.mock.calls[0][0]).toMatch(/^Suggested category: .*\.$/);
    expect(toastSuccess.mock.calls[0][0]).toContain("QA");
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["reservations"] }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /classify now/i })).not.toBeInTheDocument(),
    );
  });

  it.each(Object.entries(PURPOSE_CLASSIFY_OUTCOME_MESSAGES))(
    "on outcome %s: shows a neutral toast and keeps the button",
    async (outcome, message) => {
      server.use(
        http.post(URL, () =>
          HttpResponse.json({ reservation_id: ID, outcome, purpose_suggestion: null }),
        ),
      );
      renderModal();
      fireEvent.click(button());
      await waitFor(() => expect(toastFn).toHaveBeenCalledWith(message));
      expect(toastSuccess).not.toHaveBeenCalled();
      expect(toastError).not.toHaveBeenCalled();
      await waitFor(() => expect(button()).toBeEnabled());
      expect(button()).toHaveTextContent("Classify now");
    },
  );

  it("disables the button while the request is in flight and sends one request", async () => {
    let calls = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    server.use(
      http.post(URL, async () => {
        calls += 1;
        await gate;
        return HttpResponse.json({ reservation_id: ID, outcome: "timeout", purpose_suggestion: null });
      }),
    );
    renderModal();
    fireEvent.click(button());
    await waitFor(() => expect(button()).toBeDisabled());
    expect(button()).toHaveTextContent("Classifying...");
    fireEvent.click(button());
    release();
    await waitFor(() => expect(button()).toBeEnabled());
    expect(calls).toBe(1);
  });

  it("on 503 disabled: shows the disabled message and keeps the button", async () => {
    server.use(
      http.post(URL, () =>
        HttpResponse.json({ detail: { error: "purpose_classification_disabled" } }, { status: 503 }),
      ),
    );
    renderModal();
    fireEvent.click(button());
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(PURPOSE_CLASSIFY_DISABLED_MESSAGE));
    await waitFor(() => expect(button()).toBeEnabled());
  });

  it.each([
    ["already_suggested", PURPOSE_CLASSIFY_ALREADY_SUGGESTED_MESSAGE],
    ["not_eligible", PURPOSE_CLASSIFY_NOT_ELIGIBLE_MESSAGE],
  ])("on 409 %s: shows a plain message, hides the button, and refetches", async (error, message) => {
    server.use(http.post(URL, () => HttpResponse.json({ detail: { error } }, { status: 409 })));
    const { invalidate } = renderModal();
    fireEvent.click(button());
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(message));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["reservations"] }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /classify now/i })).not.toBeInTheDocument(),
    );
  });

  it("on 404 uses the string detail through errorDetail", async () => {
    server.use(
      http.post(URL, () => HttpResponse.json({ detail: "Reservation not found" }, { status: 404 })),
    );
    renderModal();
    fireEvent.click(button());
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Reservation not found"));
    await waitFor(() => expect(button()).toBeEnabled());
  });

  it("on an unstructured failure falls back to the generic message", async () => {
    server.use(http.post(URL, () => HttpResponse.json({}, { status: 500 })));
    renderModal();
    fireEvent.click(button());
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Failed to classify purpose"));
  });
});
