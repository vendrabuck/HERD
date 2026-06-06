import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Auth store: by default the logged-in user owns the reservation below.
// The component reads `user` via the selector form, while the shared axios
// client reads tokens via `useAuthStore.getState()` in its request/refresh
// interceptors, so the mock must support both call shapes.
let currentUserId = "owner-1";
vi.mock("@/stores/authStore", () => {
  const state = () => ({
    user: {
      id: currentUserId,
      role: "user",
      username: "alice",
      email: "alice@example.com",
    },
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

// react-hot-toast is fired on save success/failure; stub it so the calls are
// observable without rendering the toaster.
const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

import { server } from "../mocks/server";
import { ReservationStatusTab } from "@/components/reservations/ReservationStatusTab";
import type { Reservation } from "@/types/reservation.types";

const BASE: Reservation = {
  id: "11111111-2222-3333-4444-555555555555",
  user_id: "owner-1",
  owner_name: "alice",
  device_ids: ["d-1", "d-2"],
  topology_id: "abcdef0123456789",
  topology_type: "PHYSICAL",
  purpose: "fw test",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-30T12:00:00Z",
};

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  currentUserId = "owner-1";
  toastSuccess.mockReset();
  toastError.mockReset();
});

describe("ReservationStatusTab", () => {
  it("renders status, purpose, and read-only schedule fields", () => {
    renderWithProviders(<ReservationStatusTab reservation={BASE} onUpdated={vi.fn()} />);
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("fw test")).toBeInTheDocument();
    expect(screen.getByText("Edit Schedule")).toBeInTheDocument();
    // Not editing yet: no inputs present.
    expect(screen.queryByPlaceholderText("Purpose")).not.toBeInTheDocument();
  });

  it("renders a dash when purpose is null", () => {
    renderWithProviders(
      <ReservationStatusTab reservation={{ ...BASE, purpose: null }} onUpdated={vi.fn()} />,
    );
    expect(screen.getByText("-")).toBeInTheDocument();
  });

  it("hides the edit button when the viewer is not the owner", () => {
    currentUserId = "someone-else";
    renderWithProviders(<ReservationStatusTab reservation={BASE} onUpdated={vi.fn()} />);
    expect(screen.queryByText("Edit Schedule")).not.toBeInTheDocument();
  });

  it("hides the edit button on a terminal status even for the owner", () => {
    renderWithProviders(
      <ReservationStatusTab reservation={{ ...BASE, status: "COMPLETED" }} onUpdated={vi.fn()} />,
    );
    expect(screen.queryByText("Edit Schedule")).not.toBeInTheDocument();
  });

  it("reveals editable inputs when Edit Schedule is clicked and hides them on Cancel", () => {
    renderWithProviders(<ReservationStatusTab reservation={BASE} onUpdated={vi.fn()} />);

    fireEvent.click(screen.getByText("Edit Schedule"));
    expect(screen.getByPlaceholderText("Purpose")).toBeInTheDocument();
    expect(screen.getByText("Save")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByPlaceholderText("Purpose")).not.toBeInTheDocument();
    expect(screen.getByText("Edit Schedule")).toBeInTheDocument();
  });

  it("saving a changed purpose calls the update endpoint, toasts, and notifies the parent", async () => {
    let received: unknown = null;
    server.use(
      http.patch("/api/reservations/:id", async ({ request }) => {
        received = await request.json();
        return HttpResponse.json({ ...BASE, purpose: "new purpose" });
      }),
    );
    const onUpdated = vi.fn();
    // Millisecond-precise UTC end_time so only the purpose change shows up in the
    // payload (the untouched end_time round-trips identically and is omitted).
    const res = { ...BASE, end_time: "2026-06-02T00:00:00.000Z" };
    renderWithProviders(<ReservationStatusTab reservation={res} onUpdated={onUpdated} />);

    fireEvent.click(screen.getByText("Edit Schedule"));
    fireEvent.change(screen.getByPlaceholderText("Purpose"), {
      target: { value: "new purpose" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(onUpdated).toHaveBeenCalledTimes(1));
    expect(received).toEqual({ purpose: "new purpose" });
    expect(toastSuccess).toHaveBeenCalledWith("Reservation updated");
    // Editor closes on success.
    expect(screen.queryByPlaceholderText("Purpose")).not.toBeInTheDocument();
  });

  it("saving with no changes closes the editor without hitting the endpoint", async () => {
    const patch = vi.fn();
    server.use(
      http.patch("/api/reservations/:id", () => {
        patch();
        return HttpResponse.json(BASE);
      }),
    );
    const onUpdated = vi.fn();
    // The editor seeds the datetime-local field from end_time and only sends it
    // back if the minute-truncated round-trip differs from the stored value.
    // Use a millisecond-precise UTC end_time so opening then saving with no edits
    // round-trips to the identical ISO string, exercising the genuine no-op path.
    const noopRes = { ...BASE, end_time: "2026-06-02T00:00:00.000Z" };
    renderWithProviders(<ReservationStatusTab reservation={noopRes} onUpdated={onUpdated} />);

    fireEvent.click(screen.getByText("Edit Schedule"));
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(screen.getByText("Edit Schedule")).toBeInTheDocument());
    expect(patch).not.toHaveBeenCalled();
    expect(onUpdated).not.toHaveBeenCalled();
  });

  it("toasts an error and keeps the editor open when the update fails", async () => {
    server.use(
      http.patch("/api/reservations/:id", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    const onUpdated = vi.fn();
    renderWithProviders(<ReservationStatusTab reservation={BASE} onUpdated={onUpdated} />);

    fireEvent.click(screen.getByText("Edit Schedule"));
    fireEvent.change(screen.getByPlaceholderText("Purpose"), {
      target: { value: "changed" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(onUpdated).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("Purpose")).toBeInTheDocument();
  });
});
