import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

// The reservation detail modal pulls in heavy nested UI (AI tab, inventory tab,
// etc.) that is exercised elsewhere. Stub it to keep this test page-focused.
vi.mock("@/components/reservations/ReservationDetailModal", () => ({
  ReservationDetailModal: ({
    reservation,
  }: {
    reservation: { id: string } | null;
  }) =>
    reservation ? (
      <div data-testid="reservation-detail-modal">{reservation.id}</div>
    ) : null,
}));

import { server } from "../mocks/server";
import { ReservationsPage } from "@/pages/ReservationsPage";

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
  server.use(
    http.get("/api/inventory/devices", () =>
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
});
