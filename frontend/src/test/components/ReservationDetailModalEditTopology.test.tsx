import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// The Modal uses a native <dialog>. The global test setup (src/test/setup.ts)
// stubs showModal/close so they toggle the `open` attribute, which keeps the
// dialog's contents in the accessibility tree for getByRole queries.

// useNavigate is the side effect we assert on; keep the rest of react-router-dom
// real so nothing else breaks.
const navigateSpy = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigateSpy };
});

// Auth store: the logged-in user owns the reservation by default. The component
// reads `user` via the selector form.
let currentUserId = "owner-1";
vi.mock("@/stores/authStore", () => {
  const state = () => ({
    user: { id: currentUserId, role: "user", username: "alice", email: "alice@example.com" },
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

// The mutation and AI-status hooks are not under test here; stub them so the
// modal renders without a QueryClient. aiStatus is left disabled so the AI
// Assistant tab is hidden, which keeps the render focused on the header.
vi.mock("@/api/reservations", () => ({
  useCancelReservation: () => ({ mutate: vi.fn(), isPending: false }),
  useReleaseReservation: () => ({ mutate: vi.fn(), isPending: false }),
  usePurposeCategories: () => ({ data: { categories: [] } }),
  useSetPurposeCategory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/api/ai", () => ({
  useAIStatus: () => ({ data: { enabled: false } }),
}));
// The dynamic-request name lookup (issue #473) is exercised in
// ReservationDetailModal.test.tsx; stub it here for the same no-QueryClient reason.
vi.mock("@/api/templates", () => ({
  useTemplates: () => ({ data: [] }),
}));

// The tab bodies and side modals are exercised in their own tests. Stub them so
// this test isolates the header's "Edit topology" button gating.
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
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-30T12:00:00Z",
};

function renderModal(reservation: Reservation) {
  return render(
    <ReservationDetailModal
      reservation={reservation}
      deviceNames={new Map()}
      onClose={onClose}
    />,
  );
}

const onClose = vi.fn();

beforeEach(() => {
  currentUserId = "owner-1";
  navigateSpy.mockReset();
  onClose.mockReset();
});

// The fork is editable only while the reservation is ACTIVE (ADR 0006
// Decision 6), and it does not exist until the reservation activates. So the
// editable "Edit topology" affordance is ACTIVE-only, ended reservations get the
// read-only "View as-built", and a still-PENDING reservation gets neither (no
// fork yet). Both open the same fork view URL; the editor decides editable vs
// read-only from the fork's status.
describe("ReservationDetailModal Edit topology button", () => {
  it("shows Edit topology for the owner of an ACTIVE reservation with a topology_id", () => {
    renderModal(BASE);
    expect(screen.getByRole("button", { name: "Edit topology" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View as-built" })).not.toBeInTheDocument();
  });

  it("offers neither fork affordance for a still-PENDING reservation (no fork yet)", () => {
    renderModal({ ...BASE, status: "PENDING" });
    expect(screen.queryByRole("button", { name: "Edit topology" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View as-built" })).not.toBeInTheDocument();
  });

  it("hides both affordances when topology_id is null", () => {
    renderModal({ ...BASE, topology_id: null });
    expect(screen.queryByRole("button", { name: "Edit topology" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View as-built" })).not.toBeInTheDocument();
  });

  it("hides both affordances for a non-owner even with a topology_id", () => {
    currentUserId = "someone-else";
    renderModal(BASE);
    expect(screen.queryByRole("button", { name: "Edit topology" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View as-built" })).not.toBeInTheDocument();
  });

  it("shows read-only View as-built (not Edit topology) on a COMPLETED reservation", () => {
    renderModal({ ...BASE, status: "COMPLETED" });
    expect(screen.queryByRole("button", { name: "Edit topology" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View as-built" })).toBeInTheDocument();
  });

  it("shows read-only View as-built (not Edit topology) on a CANCELLED reservation", () => {
    renderModal({ ...BASE, status: "CANCELLED" });
    expect(screen.queryByRole("button", { name: "Edit topology" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View as-built" })).toBeInTheDocument();
  });

  it("closes the modal and navigates to the fork view with reservationId on click", () => {
    renderModal(BASE);
    fireEvent.click(screen.getByRole("button", { name: "Edit topology" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(navigateSpy).toHaveBeenCalledTimes(1);
    expect(navigateSpy).toHaveBeenCalledWith(
      "/topology/topo-123?reservationId=11111111-2222-3333-4444-555555555555",
    );
  });

  it("View as-built opens the same fork view for an ended reservation", () => {
    renderModal({ ...BASE, status: "COMPLETED" });
    fireEvent.click(screen.getByRole("button", { name: "View as-built" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(navigateSpy).toHaveBeenCalledWith(
      "/topology/topo-123?reservationId=11111111-2222-3333-4444-555555555555",
    );
  });
});
