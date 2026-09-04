import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// The Modal uses a native <dialog>. The global test setup (src/test/setup.ts)
// stubs showModal/close so they toggle the `open` attribute, which keeps the
// dialog's contents in the accessibility tree for getByRole queries.

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => vi.fn() };
});

// Capture toast.error so the revert-on-failure path can be asserted without
// rendering the real toaster.
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: toastError },
}));

// Auth store: id and role are mutable per test so the same reservation can be
// viewed as its owner, an admin, or an unrelated third user.
let currentUserId = "owner-1";
let currentUserRole = "user";
vi.mock("@/stores/authStore", () => {
  const state = () => ({
    user: { id: currentUserId, role: currentUserRole, username: "alice", email: "a@b.c" },
    accessToken: "test-token",
    refreshToken: "test-refresh",
    isAuthenticated: true,
    setTokens: vi.fn(),
    clearAuth: vi.fn(),
    setUser: vi.fn(),
  });
  const useAuthStore = (sel: (s: unknown) => unknown) => sel(state());
  useAuthStore.getState = state;
  return { useAuthStore };
});

// The categories list and the set-category mutation are the two hooks under
// test; everything else on this module is stubbed the same way as the sibling
// ReservationDetailModalEditTopology.test.tsx so the modal renders without a
// QueryClient.
const { setPurposeCategoryMutateAsync, purposeCategoryPendingRef } = vi.hoisted(() => ({
  setPurposeCategoryMutateAsync: vi.fn(),
  purposeCategoryPendingRef: { current: false },
}));
vi.mock("@/api/reservations", () => ({
  useCancelReservation: () => ({ mutate: vi.fn(), isPending: false }),
  useReleaseReservation: () => ({ mutate: vi.fn(), isPending: false }),
  usePurposeCategories: () => ({
    data: { categories: ["qa_regression", "training"] },
  }),
  useSetPurposeCategory: () => ({
    mutateAsync: setPurposeCategoryMutateAsync,
    isPending: purposeCategoryPendingRef.current,
  }),
}));
vi.mock("@/api/ai", () => ({
  useAIStatus: () => ({ data: { enabled: false } }),
}));
vi.mock("@/api/templates", () => ({
  useTemplates: () => ({ data: [] }),
}));

vi.mock("@/components/reservations/ReservationInventoryTab", () => ({
  ReservationInventoryTab: () => <div data-testid="inventory-tab" />,
}));
vi.mock("@/components/reservations/ReservationRoutesTab", () => ({
  ReservationRoutesTab: () => <div data-testid="routes-tab" />,
}));
vi.mock("@/components/reservations/ReservationStatusTab", () => ({
  ReservationStatusTab: () => <div data-testid="status-tab" />,
}));
vi.mock("@/components/reservations/AIAssistantTab", () => ({
  AIAssistantTab: () => <div data-testid="assistant-tab" />,
}));
vi.mock("@/components/reservations/AIApplyConfirmModal", () => ({
  AIApplyConfirmModal: () => <div data-testid="ai-apply-modal" />,
}));
vi.mock("@/components/reservations/EditDevicesModal", () => ({
  EditDevicesModal: () => <div data-testid="edit-devices-modal" />,
}));

import { ReservationDetailModal } from "@/components/reservations/ReservationDetailModal";
import type { Reservation } from "@/types/reservation.types";

const BASE: Reservation = {
  id: "11111111-2222-3333-4444-555555555555",
  user_id: "owner-1",
  owner_name: "alice",
  device_ids: ["d-1", "d-2"],
  topology_id: "topo-123",
  topology_type: "PHYSICAL",
  purpose: "fw test",
  purpose_category: null,
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-30T12:00:00Z",
};

function renderModal(reservation: Reservation) {
  return render(
    <ReservationDetailModal reservation={reservation} deviceNames={new Map()} onClose={vi.fn()} />,
  );
}

beforeEach(() => {
  currentUserId = "owner-1";
  currentUserRole = "user";
  purposeCategoryPendingRef.current = false;
  setPurposeCategoryMutateAsync.mockReset();
  toastError.mockReset();
});

describe("ReservationDetailModal purpose category", () => {
  it("shows an editable select for the owner", () => {
    renderModal(BASE);
    expect(screen.getByLabelText("Purpose category")).toBeInTheDocument();
  });

  it("shows an editable select for an admin who does not own the reservation", () => {
    currentUserId = "admin-1";
    currentUserRole = "admin";
    renderModal(BASE);
    expect(screen.getByLabelText("Purpose category")).toBeInTheDocument();
  });

  it("hides the select and shows a read-only tag for a third user", () => {
    currentUserId = "someone-else";
    currentUserRole = "user";
    renderModal(BASE);
    expect(screen.queryByLabelText("Purpose category")).not.toBeInTheDocument();
    expect(screen.getByText("Unclassified")).toBeInTheDocument();
  });

  it("remains editable on a terminal (COMPLETED) reservation for the owner", () => {
    renderModal({ ...BASE, status: "COMPLETED" });
    expect(screen.getByLabelText("Purpose category")).toBeInTheDocument();
  });

  it("shows the current category label as the read-only tag for a non-owner, non-admin", () => {
    currentUserId = "someone-else";
    renderModal({ ...BASE, purpose_category: "training" });
    expect(screen.getByText("Training")).toBeInTheDocument();
  });

  it("renders the fetched category options with their labels in the select", () => {
    renderModal(BASE);
    const select = screen.getByLabelText("Purpose category");
    expect(within(select).getByText("Unclassified")).toBeInTheDocument();
    expect(within(select).getByText("QA and regression")).toBeInTheDocument();
    expect(within(select).getByText("Training")).toBeInTheDocument();
  });

  it("optimistically updates on a successful change and does not toast an error", async () => {
    setPurposeCategoryMutateAsync.mockResolvedValue({ ...BASE, purpose_category: "training" });
    renderModal(BASE);
    const select = screen.getByLabelText("Purpose category") as HTMLSelectElement;

    fireEvent.change(select, { target: { value: "training" } });

    expect(select.value).toBe("training");
    await waitFor(() =>
      expect(setPurposeCategoryMutateAsync).toHaveBeenCalledWith({
        id: BASE.id,
        purposeCategory: "training",
      }),
    );
    expect(toastError).not.toHaveBeenCalled();
    expect(select.value).toBe("training");
  });

  it("reverts to the previous value and toasts an error when the PATCH fails", async () => {
    setPurposeCategoryMutateAsync.mockRejectedValue({
      response: { data: { detail: "not allowed" } },
    });
    renderModal(BASE);
    const select = screen.getByLabelText("Purpose category") as HTMLSelectElement;

    // BASE has purpose_category: null, so the select starts on "Unclassified".
    fireEvent.change(select, { target: { value: "qa_regression" } });
    expect(select.value).toBe("qa_regression");

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("not allowed"));
    expect(select.value).toBe("");
  });

  it("falls back to a generic error message when the server sends no detail", async () => {
    setPurposeCategoryMutateAsync.mockRejectedValue(new Error("network down"));
    renderModal(BASE);
    const select = screen.getByLabelText("Purpose category") as HTMLSelectElement;

    fireEvent.change(select, { target: { value: "training" } });

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Failed to update purpose category"),
    );
    expect(select.value).toBe("");
  });

  it("resets the displayed value when switching to a different reservation", () => {
    const { rerender } = render(
      <ReservationDetailModal reservation={BASE} deviceNames={new Map()} onClose={vi.fn()} />,
    );
    expect((screen.getByLabelText("Purpose category") as HTMLSelectElement).value).toBe("");

    const other: Reservation = {
      ...BASE,
      id: "99999999-8888-7777-6666-555555555555",
      purpose_category: "qa_regression",
    };
    rerender(
      <ReservationDetailModal reservation={other} deviceNames={new Map()} onClose={vi.fn()} />,
    );
    expect((screen.getByLabelText("Purpose category") as HTMLSelectElement).value).toBe(
      "qa_regression",
    );
  });
});
