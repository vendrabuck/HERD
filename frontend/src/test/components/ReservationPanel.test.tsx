import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: { success: toastSuccess, error: toastError },
}));

import { server } from "../mocks/server";
import { ReservationPanel } from "@/components/reservations/ReservationPanel";

const RESV = {
  id: "11111111-1111-1111-1111-111111111111",
  user_id: "22222222-2222-2222-2222-222222222222",
  owner_name: "tester",
  start_time: "2026-05-01T00:00:00+00:00",
  end_time: "2026-05-02T00:00:00+00:00",
  status: "ACTIVE",
  topology_type: "PHYSICAL",
  topology_id: null,
  purpose: null,
  device_ids: ["33333333-3333-3333-3333-333333333333"],
};

function renderPanel(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("ReservationPanel cancel confirmation", () => {
  beforeEach(() => {
    toastSuccess.mockClear();
    toastError.mockClear();
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [RESV], total: 1, skip: 0, limit: 500 }),
      ),
    );
  });

  it("clicking Cancel opens a confirm dialog and does NOT delete immediately", async () => {
    let deleteCalled = false;
    server.use(
      http.delete("/api/reservations/:id", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderPanel(<ReservationPanel />);
    const cancelBtn = await screen.findByLabelText(/Cancel reservation/);
    fireEvent.click(cancelBtn);

    // The confirm dialog appears; the DELETE has not been fired yet.
    expect(await screen.findByText("Cancel Reservation")).toBeInTheDocument();
    expect(deleteCalled).toBe(false);
  });

  it("confirming the dialog fires the cancel mutation", async () => {
    let deleteCalled = false;
    server.use(
      http.delete("/api/reservations/:id", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderPanel(<ReservationPanel />);
    fireEvent.click(await screen.findByLabelText(/Cancel reservation/));
    // Click the dialog's confirm button (label "Cancel reservation" is the
    // aria-label of the row button too, so scope to the dialog's button text).
    const confirmBtn = await screen.findByRole("button", { name: "Cancel reservation" });
    fireEvent.click(confirmBtn);

    await waitFor(() => expect(deleteCalled).toBe(true));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Reservation cancelled"));
  });
});
