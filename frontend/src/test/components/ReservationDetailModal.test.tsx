import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

// The current user owns the reservation under test (user_id matches), so the
// owner-only Release/Cancel and Edit Resources affordances are reachable.
// useAuthStore is used two ways: as a selector hook in components and via
// .getState() in the axios client interceptor. The mock must satisfy both, or
// the interceptor throws and mutation requests never reach MSW. Everything is
// kept inside the factory because vi.mock is hoisted above top-level consts.
vi.mock("@/stores/authStore", () => {
  const authState = {
    user: { id: "user-1", role: "admin", username: "admin", email: "a@b.c" },
    accessToken: null,
    refreshToken: null,
    setTokens: () => {},
    clearAuth: () => {},
  };
  const useAuthStore = (selector: (state: typeof authState) => unknown) =>
    selector(authState);
  useAuthStore.getState = () => authState;
  return { useAuthStore };
});

// The assistant tab visibility is driven by the AI status hook; default to
// disabled and override per-test where the assistant branch is exercised.
const aiStatusMock = vi.fn();
vi.mock("@/api/ai", () => ({
  useAIStatus: () => aiStatusMock(),
}));

// The non-details tabs and secondary modals render heavy nested UI exercised in
// their own test files. Stub them so this test stays focused on the modal's own
// tab routing, owner gating, and cancel/release wiring.
vi.mock("@/components/reservations/ReservationInventoryTab", () => ({
  ReservationInventoryTab: ({ deviceIds }: { deviceIds: string[] }) => (
    <div data-testid="inventory-tab">inventory {deviceIds.length}</div>
  ),
}));
vi.mock("@/components/reservations/ReservationRoutesTab", () => ({
  ReservationRoutesTab: () => <div data-testid="routes-tab">routes</div>,
}));
vi.mock("@/components/reservations/ReservationStatusTab", () => ({
  ReservationStatusTab: () => <div data-testid="status-tab">schedule</div>,
}));
vi.mock("@/components/reservations/AIAssistantTab", () => ({
  AIAssistantTab: () => <div data-testid="assistant-tab">assistant</div>,
}));
vi.mock("@/components/reservations/AIApplyConfirmModal", () => ({
  AIApplyConfirmModal: () => <div data-testid="apply-modal" />,
}));
vi.mock("@/components/reservations/EditDevicesModal", () => ({
  EditDevicesModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="edit-devices-modal">edit devices</div> : null,
}));

import { server } from "../mocks/server";
import { ReservationDetailModal } from "@/components/reservations/ReservationDetailModal";
import type { Reservation } from "@/types/reservation.types";

const RESERVATION: Reservation = {
  id: "res-1",
  user_id: "user-1",
  owner_name: "alice",
  device_ids: ["d-1", "d-2"],
  topology_id: "topo-1",
  topology_type: "PHYSICAL",
  purpose: "fw test",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-01T00:00:00Z",
};

const DEVICE_NAMES = new Map<string, string>([
  ["d-1", "firewall-a"],
  ["d-2", "firewall-b"],
]);

function renderModal(overrides: Partial<Reservation> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onClose = vi.fn();
  const utils = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ReservationDetailModal
          reservation={{ ...RESERVATION, ...overrides }}
          deviceNames={DEVICE_NAMES}
          onClose={onClose}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, onClose };
}

beforeEach(() => {
  aiStatusMock.mockReturnValue({ data: { enabled: false } });
});

describe("ReservationDetailModal", () => {
  it("renders nothing when there is no reservation", () => {
    const { container } = render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <ReservationDetailModal
            reservation={null}
            deviceNames={DEVICE_NAMES}
            onClose={vi.fn()}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders details with owner, status, and resolved device names", () => {
    renderModal();
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("fw test")).toBeInTheDocument();
    expect(screen.getByText("Devices (2)")).toBeInTheDocument();
    expect(screen.getByText("firewall-a")).toBeInTheDocument();
    expect(screen.getByText("firewall-b")).toBeInTheDocument();
  });

  it("hides the AI assistant tab when the AI status is disabled", () => {
    renderModal();
    expect(screen.queryByText("AI Assistant")).not.toBeInTheDocument();
  });

  it("shows the AI assistant tab and renders it when AI is enabled", () => {
    aiStatusMock.mockReturnValue({ data: { enabled: true } });
    renderModal();
    const assistantTabButton = screen.getByRole("button", { name: "AI Assistant", hidden: true });
    fireEvent.click(assistantTabButton);
    expect(screen.getByTestId("assistant-tab")).toBeInTheDocument();
  });

  it("switches to the inventory tab and forwards the device ids", () => {
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: "Inventory", hidden: true }));
    expect(screen.getByTestId("inventory-tab")).toHaveTextContent("inventory 2");
    expect(screen.queryByText("Owner")).not.toBeInTheDocument();
  });

  it("opens the edit devices modal from the Edit Resources button", () => {
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: "Edit Resources", hidden: true }));
    expect(screen.getByTestId("edit-devices-modal")).toBeInTheDocument();
  });

  it("hides Release and Cancel for a non-owner", () => {
    renderModal({ user_id: "someone-else" });
    expect(screen.queryByRole("button", { name: "Release", hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel", hidden: true })).not.toBeInTheDocument();
  });

  it("hides Release and Cancel when the reservation is not ACTIVE", () => {
    renderModal({ status: "PENDING" });
    expect(screen.queryByRole("button", { name: "Release", hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel", hidden: true })).not.toBeInTheDocument();
  });

  it("cancels the reservation after confirming and closes on success", async () => {
    let deleted = false;
    server.use(
      http.delete("/api/reservations/res-1", () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { onClose } = renderModal();
    fireEvent.click(screen.getByRole("button", { name: "Cancel", hidden: true }));

    // The destructive confirm dialog must intercept before the API is hit.
    const confirmButton = screen.getByRole("button", { name: "Cancel reservation", hidden: true });
    expect(deleted).toBe(false);
    fireEvent.click(confirmButton);

    await waitFor(() => expect(deleted).toBe(true));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("releases the reservation and closes on success", async () => {
    let released = false;
    server.use(
      http.put("/api/reservations/res-1/release", () => {
        released = true;
        return HttpResponse.json({ ...RESERVATION, status: "COMPLETED" });
      }),
    );

    const { onClose } = renderModal();
    fireEvent.click(screen.getByRole("button", { name: "Release", hidden: true }));

    await waitFor(() => expect(released).toBe(true));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});
