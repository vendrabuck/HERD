import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  // Toggle the `open` property so tests can observe whether a <dialog> (the
  // create-reservation modal) is actually shown; jsdom has no real dialog
  // behavior.
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

// The reservation detail modal pulls in heavy nested UI (AI tab, inventory tab,
// etc.) that is exercised elsewhere. Stub it to keep this test page-focused.
// The close button is wired to the real onClose prop so the page's own
// onClose callback (setSelectedReservation(null)) stays under test.
vi.mock("@/components/reservations/ReservationDetailModal", () => ({
  ReservationDetailModal: ({
    reservation,
    onClose,
  }: {
    reservation: { id: string } | null;
    onClose: () => void;
  }) =>
    reservation ? (
      <div data-testid="reservation-detail-modal">
        {reservation.id}
        <button onClick={onClose}>close-detail-modal</button>
      </div>
    ) : null,
}));

import { server } from "../mocks/server";
import { ReservationsPage } from "@/pages/ReservationsPage";
import { useAuthStore } from "@/stores/authStore";

function setRole(role: string | null) {
  useAuthStore.setState({
    user: role
      ? {
          id: "1",
          email: "a@b.c",
          username: "u",
          is_active: true,
          role,
          created_at: "2026-01-01T00:00:00Z",
        }
      : null,
  });
}

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const RESERVATION = {
  id: "11111111-2222-3333-4444-555555555555",
  user_id: "user-1",
  owner_name: "alice",
  status: "ACTIVE",
  topology_id: "abcdef0123456789",
  topology_type: "PHYSICAL",
  device_ids: ["d-1", "d-2"],
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  purpose: "fw test",
};

beforeEach(() => {
  setRole(null);
  server.use(
    http.get("/api/inventory/devices", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
    // The create-reservation modal fetches dynamic templates once opened.
    http.get("/api/inventory/templates", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
  );
});

describe("ReservationsPage", () => {
  it("shows the loading state", () => {
    server.use(
      http.get("/api/reservations/", async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<ReservationsPage />);
    expect(screen.getByText(/Loading reservations/i)).toBeInTheDocument();
  });

  it("renders an empty state when there are no reservations", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);
    await waitFor(() =>
      expect(screen.getByText("No reservations yet")).toBeInTheDocument(),
    );
  });

  it("renders a reservation row with owner, status, and devices", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({
          items: [RESERVATION],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );
    renderWithProviders(<ReservationsPage />);
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("2 devices")).toBeInTheDocument();
    expect(screen.getByText("fw test")).toBeInTheDocument();
  });

  it("clicking a row opens the detail modal", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({
          items: [RESERVATION],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );
    renderWithProviders(<ReservationsPage />);
    const ownerCell = await screen.findByText("alice");
    fireEvent.click(ownerCell);
    expect(screen.getByTestId("reservation-detail-modal")).toHaveTextContent(
      RESERVATION.id,
    );
  });

  it("hides the all-reservations toggle from non-admins", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 }),
      ),
    );
    setRole("user");
    renderWithProviders(<ReservationsPage />);
    await waitFor(() =>
      expect(screen.getByText("No reservations yet")).toBeInTheDocument(),
    );
    expect(
      screen.queryByLabelText("All reservations"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("My Reservations")).toBeInTheDocument();
  });

  it("admin toggle switches the list query to all=true", async () => {
    const seenAllParams: string[] = [];
    server.use(
      http.get("/api/reservations/", ({ request }) => {
        seenAllParams.push(new URL(request.url).searchParams.get("all") ?? "");
        return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 });
      }),
    );
    setRole("admin");
    renderWithProviders(<ReservationsPage />);

    const toggle = await screen.findByLabelText("All reservations");
    // Default view is the caller's own reservations: no all param sent.
    await waitFor(() => expect(seenAllParams.length).toBeGreaterThan(0));
    expect(seenAllParams.every((v) => v === "")).toBe(true);

    fireEvent.click(toggle);
    // After toggling, the refetch carries all=true and the header relabels.
    await waitFor(() =>
      expect(seenAllParams.some((v) => v === "true")).toBe(true),
    );
    expect(screen.getByText("All Reservations")).toBeInTheDocument();
  });

  it("renders an error state when the fetch fails", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);
    await waitFor(() =>
      expect(
        screen.getByText("Failed to load reservations"),
      ).toBeInTheDocument(),
    );
  });

  it("releases an ACTIVE reservation without navigating to its detail modal", async () => {
    const releaseCalls: string[] = [];
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [RESERVATION], total: 1, skip: 0, limit: 50 }),
      ),
      http.put("/api/reservations/:id/release", ({ params }) => {
        releaseCalls.push(params.id as string);
        return HttpResponse.json({ ...RESERVATION, status: "COMPLETED" });
      }),
    );
    renderWithProviders(<ReservationsPage />);

    const releaseButton = await screen.findByRole("button", {
      name: `Release reservation ${RESERVATION.id.slice(0, 8)}`,
    });
    fireEvent.click(releaseButton);

    await waitFor(() => expect(releaseCalls).toEqual([RESERVATION.id]));
    // The row's own onClick (which opens the detail modal) must not have
    // fired: the cell's stopPropagation swallowed the click bubble.
    expect(screen.queryByTestId("reservation-detail-modal")).not.toBeInTheDocument();
  });

  it("cancels an ACTIVE reservation through the confirm dialog, and Keep aborts it", async () => {
    const cancelCalls: string[] = [];
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [RESERVATION], total: 1, skip: 0, limit: 50 }),
      ),
      http.delete("/api/reservations/:id", ({ params }) => {
        cancelCalls.push(params.id as string);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<ReservationsPage />);

    const cancelButton = await screen.findByRole("button", {
      name: `Cancel reservation ${RESERVATION.id.slice(0, 8)}`,
    });
    fireEvent.click(cancelButton);

    // The confirm dialog opens; Keep reservation backs out without calling
    // the API.
    const keepButton = await screen.findByRole("button", { name: "Keep reservation" });
    fireEvent.click(keepButton);
    expect(screen.queryByRole("button", { name: "Keep reservation" })).not.toBeInTheDocument();
    expect(cancelCalls).toEqual([]);

    // Re-opening and confirming actually cancels.
    fireEvent.click(cancelButton);
    const confirmButton = await screen.findByRole("button", { name: "Cancel reservation" });
    fireEvent.click(confirmButton);

    await waitFor(() => expect(cancelCalls).toEqual([RESERVATION.id]));
    expect(screen.queryByRole("button", { name: "Cancel reservation" })).not.toBeInTheDocument();
  });

  it("closes the detail modal when its onClose fires", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [RESERVATION], total: 1, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);

    const ownerCell = await screen.findByText("alice");
    fireEvent.click(ownerCell);
    const modal = screen.getByTestId("reservation-detail-modal");
    expect(modal).toHaveTextContent(RESERVATION.id);

    // The mocked modal exposes a close button wired to the real onClose prop.
    fireEvent.click(screen.getByRole("button", { name: "close-detail-modal" }));
    expect(screen.queryByTestId("reservation-detail-modal")).not.toBeInTheDocument();
  });

  it("shows a New Reservation button that opens the create modal", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);
    await waitFor(() =>
      expect(screen.getByText("No reservations yet")).toBeInTheDocument(),
    );

    // With no reservation rows there is exactly one <dialog>: the create
    // modal, closed until the button is clicked.
    const dialog = document.querySelector("dialog");
    expect(dialog?.open).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "New Reservation" }));
    expect(dialog?.open).toBe(true);
    expect(screen.getByText("Create Reservation")).toBeInTheDocument();
    // The non-canvas entry point preselects no devices.
    expect(screen.getByText("0 devices selected")).toBeInTheDocument();

    // Closing via Cancel returns the dialog to its closed state.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(dialog?.open).toBe(false);
  });
});
