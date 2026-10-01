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

// The sort controls (issue #844) persist through usePreferencesStore, which
// best-effort PATCHes the backend on every change. Mock that transport (the
// InventoryPage.test.tsx pattern) so sort-control tests assert on the queued
// payload directly instead of racing a real network call.
const { patchPreferencesMock } = vi.hoisted(() => ({ patchPreferencesMock: vi.fn() }));
vi.mock("@/api/userProfile", () => ({
  getPreferences: vi.fn(),
  patchPreferences: patchPreferencesMock,
  resetPreferences: vi.fn(),
}));

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

// Bulk Cancel and Release (issue #843) report through toast; capture the calls.
const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({ default: { success: toastSuccess, error: toastError } }));

import { server } from "../mocks/server";
import { ReservationsPage } from "@/pages/ReservationsPage";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";

function setRole(role: string | null, id = "1") {
  useAuthStore.setState({
    user: role
      ? {
          id,
          email: "a@b.c",
          username: "u",
          is_active: true,
          role,
          created_at: "2026-01-01T00:00:00Z",
        }
      : null,
  });
}

function renderWithProviders(
  node: ReactNode,
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  }),
) {
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

function reservationWithStatus(status: string) {
  return { ...RESERVATION, status };
}

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
  toastSuccess.mockReset();
  toastError.mockReset();
  patchPreferencesMock.mockReset();
  patchPreferencesMock.mockResolvedValue({
    user_id: "u",
    saved_filters: {},
    page_sizes: {},
    extras: {},
    updated_at: "",
  });
  usePreferencesStore.getState().clear();
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

  it("shows Cancel but not Release for a PENDING reservation (issue #841)", async () => {
    const pending = reservationWithStatus("PENDING");
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [pending], total: 1, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);

    await screen.findByRole("button", {
      name: `Cancel reservation ${pending.id.slice(0, 8)}`,
    });
    expect(
      screen.queryByRole("button", {
        name: `Release reservation ${pending.id.slice(0, 8)}`,
      }),
    ).not.toBeInTheDocument();
  });

  it("shows Cancel but not Release for a PENDING_PROVISION reservation", async () => {
    const pendingProvision = reservationWithStatus("PENDING_PROVISION");
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [pendingProvision], total: 1, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);

    await screen.findByRole("button", {
      name: `Cancel reservation ${pendingProvision.id.slice(0, 8)}`,
    });
    expect(
      screen.queryByRole("button", {
        name: `Release reservation ${pendingProvision.id.slice(0, 8)}`,
      }),
    ).not.toBeInTheDocument();
  });

  it("shows both Release and Cancel for an ACTIVE reservation (regression guard)", async () => {
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [RESERVATION], total: 1, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);

    await screen.findByRole("button", {
      name: `Release reservation ${RESERVATION.id.slice(0, 8)}`,
    });
    expect(
      screen.getByRole("button", {
        name: `Cancel reservation ${RESERVATION.id.slice(0, 8)}`,
      }),
    ).toBeInTheDocument();
  });

  it("shows neither Release nor Cancel for a terminal (CANCELLED) reservation", async () => {
    const cancelled = reservationWithStatus("CANCELLED");
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [cancelled], total: 1, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<ReservationsPage />);

    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());
    expect(
      screen.queryByRole("button", {
        name: `Cancel reservation ${cancelled.id.slice(0, 8)}`,
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: `Release reservation ${cancelled.id.slice(0, 8)}`,
      }),
    ).not.toBeInTheDocument();
  });

  it("cancels a PENDING reservation through the confirm dialog", async () => {
    const pending = reservationWithStatus("PENDING");
    const cancelCalls: string[] = [];
    server.use(
      http.get("/api/reservations/", () =>
        HttpResponse.json({ items: [pending], total: 1, skip: 0, limit: 50 }),
      ),
      http.delete("/api/reservations/:id", ({ params }) => {
        cancelCalls.push(params.id as string);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<ReservationsPage />);

    const cancelButton = await screen.findByRole("button", {
      name: `Cancel reservation ${pending.id.slice(0, 8)}`,
    });
    fireEvent.click(cancelButton);

    const confirmButton = await screen.findByRole("button", { name: "Cancel reservation" });
    fireEvent.click(confirmButton);

    await waitFor(() => expect(cancelCalls).toEqual([pending.id]));
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

  describe("column sort controls (issue #844)", () => {
    interface SeenRequest {
      skip: string | null;
      sortBy: string | null;
      sortDir: string | null;
    }

    function captureRequests(): SeenRequest[] {
      const seen: SeenRequest[] = [];
      server.use(
        http.get("/api/reservations/", ({ request }) => {
          const params = new URL(request.url).searchParams;
          seen.push({
            skip: params.get("skip"),
            sortBy: params.get("sort_by"),
            sortDir: params.get("sort_dir"),
          });
          // total=100 with one item keeps the Pagination nav visible (it
          // hides at total <= limit) without needing 100 seeded rows.
          return HttpResponse.json({ items: [RESERVATION], total: 100, skip: 0, limit: 50 });
        }),
      );
      return seen;
    }

    it("sends no sort params by default and marks every sortable heading aria-sort=none", async () => {
      const seen = captureRequests();
      renderWithProviders(<ReservationsPage />);
      await waitFor(() => expect(seen.length).toBeGreaterThan(0));
      expect(seen[0].sortBy).toBeNull();
      expect(seen[0].sortDir).toBeNull();

      for (const name of ["Owner", "Status", "Period", "Purpose"]) {
        expect(screen.getByRole("columnheader", { name })).toHaveAttribute(
          "aria-sort",
          "none",
        );
      }
    });

    it("clicking a heading sorts ascending, sends sort_by/sort_dir, and resets to page 1", async () => {
      const seen = captureRequests();
      renderWithProviders(<ReservationsPage />);
      await waitFor(() => expect(seen.length).toBeGreaterThan(0));

      // Move off the first page first so the reset is actually observable.
      fireEvent.click(screen.getByText("Next"));
      await waitFor(() => expect(seen.some((r) => r.skip === "50")).toBe(true));

      fireEvent.click(screen.getByRole("button", { name: "Owner" }));
      await waitFor(() =>
        expect(seen.some((r) => r.sortBy === "user_id" && r.sortDir === "asc")).toBe(true),
      );
      const lastAscending = seen[seen.length - 1];
      expect(lastAscending.skip).toBe("0");
      expect(screen.getByRole("columnheader", { name: "Owner" })).toHaveAttribute(
        "aria-sort",
        "ascending",
      );
    });

    it("cycles a heading through ascending, descending, and back to the default on a third click", async () => {
      const seen = captureRequests();
      renderWithProviders(<ReservationsPage />);
      await waitFor(() => expect(seen.length).toBeGreaterThan(0));

      const heading = () => screen.getByRole("button", { name: "Status" });
      const cell = () => screen.getByRole("columnheader", { name: "Status" });

      fireEvent.click(heading());
      await waitFor(() =>
        expect(seen.some((r) => r.sortBy === "status" && r.sortDir === "asc")).toBe(true),
      );
      expect(cell()).toHaveAttribute("aria-sort", "ascending");

      fireEvent.click(heading());
      await waitFor(() =>
        expect(seen.some((r) => r.sortBy === "status" && r.sortDir === "desc")).toBe(true),
      );
      expect(cell()).toHaveAttribute("aria-sort", "descending");

      fireEvent.click(heading());
      await waitFor(() =>
        expect(seen[seen.length - 1]).toEqual({ skip: "0", sortBy: null, sortDir: null }),
      );
      expect(cell()).toHaveAttribute("aria-sort", "none");
    });

    it("switching to a different heading replaces the previous sort", async () => {
      const seen = captureRequests();
      renderWithProviders(<ReservationsPage />);
      await waitFor(() => expect(seen.length).toBeGreaterThan(0));

      fireEvent.click(screen.getByRole("button", { name: "Owner" }));
      await waitFor(() => expect(seen.some((r) => r.sortBy === "user_id")).toBe(true));

      fireEvent.click(screen.getByRole("button", { name: "Purpose" }));
      await waitFor(() =>
        expect(
          seen.some((r) => r.sortBy === "purpose_category" && r.sortDir === "asc"),
        ).toBe(true),
      );
      expect(screen.getByRole("columnheader", { name: "Owner" })).toHaveAttribute(
        "aria-sort",
        "none",
      );
      expect(screen.getByRole("columnheader", { name: "Purpose" })).toHaveAttribute(
        "aria-sort",
        "ascending",
      );
    });

    it("persists the chosen sort through the preferences store", async () => {
      const seen = captureRequests();
      renderWithProviders(<ReservationsPage />);
      await waitFor(() => expect(seen.length).toBeGreaterThan(0));

      fireEvent.click(screen.getByRole("button", { name: "Period" }));
      await waitFor(() =>
        expect(seen.some((r) => r.sortBy === "start_time" && r.sortDir === "asc")).toBe(true),
      );

      expect(usePreferencesStore.getState().getSortState("reservations")).toEqual({
        sortBy: "start_time",
        sortDir: "asc",
      });
      await waitFor(() => expect(patchPreferencesMock).toHaveBeenCalled());
      const lastCall = patchPreferencesMock.mock.calls[patchPreferencesMock.mock.calls.length - 1];
      expect(lastCall[0].extras).toEqual({
        "sort:reservations": { sortBy: "start_time", sortDir: "asc" },
      });
    });

    it("a previously persisted sort is applied on load", async () => {
      usePreferencesStore.setState({
        extras: { "sort:reservations": { sortBy: "purpose_category", sortDir: "desc" } },
      });
      const seen = captureRequests();
      renderWithProviders(<ReservationsPage />);
      await waitFor(() =>
        expect(
          seen.some((r) => r.sortBy === "purpose_category" && r.sortDir === "desc"),
        ).toBe(true),
      );
      expect(screen.getByRole("columnheader", { name: "Purpose" })).toHaveAttribute(
        "aria-sort",
        "descending",
      );
    });
  });
  describe("bulk cancel and release (issue #843)", () => {
    const ME = "me-id";
    const uuid = (n: number) => `0000000${n}-2222-3333-4444-555555555555`;
    const row = (n: number, status: string, userId = ME) => ({
      ...RESERVATION,
      id: uuid(n),
      status,
      user_id: userId,
      owner_name: `owner${n}`,
      purpose: `purpose ${n}`,
    });
    const rowBox = (n: number) =>
      screen.getByRole("checkbox", { name: `Select reservation ${uuid(n).slice(0, 8)}` });
    const selectAllBox = () =>
      screen.getByRole("checkbox", { name: "Select all reservations on this page" });

    function serveList(items: ReturnType<typeof row>[], total = items.length) {
      const seen: URLSearchParams[] = [];
      server.use(
        http.get("/api/reservations/", ({ request }) => {
          seen.push(new URL(request.url).searchParams);
          return HttpResponse.json({ items, total, skip: 0, limit: 50 });
        }),
      );
      return seen;
    }

    beforeEach(() => setRole("user", ME));

    it("a row checkbox selects without opening the detail modal", async () => {
      serveList([row(1, "ACTIVE")]);
      renderWithProviders(<ReservationsPage />);
      const box = await screen.findByRole("checkbox", { name: /Select reservation/ });
      fireEvent.click(box);
      expect(box).toBeChecked();
      expect(screen.queryByTestId("reservation-detail-modal")).not.toBeInTheDocument();
      expect(screen.getByRole("status", { name: "Selection" })).toHaveTextContent("1 selected");
    });

    it("select-all takes the current page only and goes indeterminate on a partial selection", async () => {
      // total 100 with two rows: the page holds two, the list holds more.
      serveList([row(1, "ACTIVE"), row(2, "PENDING")], 100);
      renderWithProviders(<ReservationsPage />);
      await screen.findByText("purpose 1");

      fireEvent.click(selectAllBox());
      expect(rowBox(1)).toBeChecked();
      expect(rowBox(2)).toBeChecked();
      expect(screen.getByRole("status", { name: "Selection" })).toHaveTextContent("2 selected");
      expect((selectAllBox() as HTMLInputElement).indeterminate).toBe(false);

      fireEvent.click(rowBox(2));
      expect((selectAllBox() as HTMLInputElement).indeterminate).toBe(true);
      expect(selectAllBox()).not.toBeChecked();

      fireEvent.click(selectAllBox());
      expect(rowBox(2)).toBeChecked();
      fireEvent.click(selectAllBox());
      expect(rowBox(1)).not.toBeChecked();
      expect(screen.getByRole("status", { name: "Selection" })).not.toHaveTextContent("selected");
    });

    it("clears the selection when the page changes", async () => {
      const seen = serveList([row(1, "ACTIVE")], 100);
      renderWithProviders(<ReservationsPage />);
      fireEvent.click(await screen.findByRole("checkbox", { name: /Select reservation/ }));
      expect(screen.getByRole("status", { name: "Selection" })).toHaveTextContent("1 selected");

      fireEvent.click(screen.getByText("Next"));
      await waitFor(() => expect(seen.some((p) => p.get("skip") === "50")).toBe(true));
      await waitFor(() =>
        expect(screen.getByRole("status", { name: "Selection" })).not.toHaveTextContent("selected"),
      );
      fireEvent.click(screen.getByText("Prev"));
      await waitFor(() => expect(seen.filter((p) => p.get("skip") === "0").length).toBeGreaterThan(0));
      expect(rowBox(1)).not.toBeChecked();
    });

    it("clears the selection when the sort changes", async () => {
      serveList([row(1, "ACTIVE")]);
      renderWithProviders(<ReservationsPage />);
      fireEvent.click(await screen.findByRole("checkbox", { name: /Select reservation/ }));
      fireEvent.click(screen.getByRole("button", { name: "Status" }));
      await waitFor(() => expect(rowBox(1)).not.toBeChecked());
      expect(screen.getByRole("status", { name: "Selection" })).not.toHaveTextContent("selected");
    });

    it("clears the selection when the all-reservations toggle changes", async () => {
      serveList([row(1, "ACTIVE")]);
      setRole("admin", ME);
      renderWithProviders(<ReservationsPage />);
      fireEvent.click(await screen.findByRole("checkbox", { name: /Select reservation/ }));
      fireEvent.click(screen.getByLabelText("All reservations"));
      await waitFor(() => expect(rowBox(1)).not.toBeChecked());
      expect(screen.getByRole("status", { name: "Selection" })).not.toHaveTextContent("selected");
    });

    it("the confirmation counts what will run and what will be skipped, and why", async () => {
      const calls: string[] = [];
      serveList([
        row(1, "ACTIVE"),
        row(2, "PENDING"),
        row(3, "CANCELLED"),
        row(4, "ACTIVE", "another-user"),
      ]);
      server.use(
        http.delete("/api/reservations/:id", ({ params }) => {
          calls.push(params.id as string);
          return new HttpResponse(null, { status: 204 });
        }),
      );
      renderWithProviders(<ReservationsPage />);
      await screen.findByText("purpose 1");
      fireEvent.click(selectAllBox());
      fireEvent.click(screen.getByRole("button", { name: "Cancel selected" }));

      const desc = await screen.findByText(/Cancel 2 of the 4 selected reservations\?/);
      expect(desc).toHaveTextContent("This releases their devices and cannot be undone.");
      expect(desc).toHaveTextContent("2 will be skipped: 1 already finished, 1 not yours.");

      // Keeping aborts: nothing is sent.
      fireEvent.click(screen.getByRole("button", { name: "Keep reservations" }));
      expect(calls).toEqual([]);

      fireEvent.click(screen.getByRole("button", { name: "Cancel selected" }));
      fireEvent.click(await screen.findByRole("button", { name: "Cancel 2 reservations" }));
      await waitFor(() => expect(calls.sort()).toEqual([uuid(1), uuid(2)]));
      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Cancelled 2 reservations"));
      // The two cancelled rows leave the selection; the two skipped rows stay.
      await waitFor(() =>
        expect(screen.getByRole("status", { name: "Selection" })).toHaveTextContent("2 selected"),
      );
    });

    it("disables an action with an explanation when no selected row is eligible", async () => {
      serveList([row(1, "PENDING"), row(2, "COMPLETED")]);
      renderWithProviders(<ReservationsPage />);
      await screen.findByText("purpose 1");
      fireEvent.click(selectAllBox());

      const release = screen.getByRole("button", { name: "Release selected" });
      expect(release).toBeDisabled();
      expect(screen.getByRole("status", { name: "Selection" })).toHaveTextContent(
        "None of the selected reservations can be released: 1 already finished, 1 not active.",
      );
      fireEvent.click(release);
      expect(screen.queryByRole("button", { name: /^Release \d+ reservation/ })).not.toBeInTheDocument();
      // Cancel is still available for the PENDING row.
      expect(screen.getByRole("button", { name: "Cancel selected" })).toBeEnabled();
    });

    it("disables the action buttons while the fan-out is in flight", async () => {
      let release!: () => void;
      const gate = new Promise<void>((resolve) => (release = resolve));
      serveList([row(1, "ACTIVE")]);
      server.use(
        http.delete("/api/reservations/:id", async () => {
          await gate;
          return new HttpResponse(null, { status: 204 });
        }),
      );
      renderWithProviders(<ReservationsPage />);
      fireEvent.click(await screen.findByRole("checkbox", { name: /Select reservation/ }));
      fireEvent.click(screen.getByRole("button", { name: "Cancel selected" }));
      fireEvent.click(await screen.findByRole("button", { name: "Cancel 1 reservation" }));

      await waitFor(() => expect(screen.getByRole("button", { name: "Cancel selected" })).toBeDisabled());
      expect(screen.getByRole("button", { name: "Release selected" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Clear selection" })).toBeDisabled();

      release();
      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Cancelled 1 reservation"));
    });

    it("keeps failed rows selected, reports the counts and reason, and invalidates once", async () => {
      serveList([row(1, "ACTIVE"), row(2, "ACTIVE"), row(3, "ACTIVE")]);
      server.use(
        http.delete("/api/reservations/:id", ({ params }) =>
          params.id === uuid(2)
            ? HttpResponse.json({ detail: "Reservation not found" }, { status: 404 })
            : new HttpResponse(null, { status: 204 }),
        ),
      );
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      });
      const invalidate = vi.spyOn(client, "invalidateQueries");
      renderWithProviders(<ReservationsPage />, client);
      await screen.findByText("purpose 1");
      fireEvent.click(selectAllBox());
      fireEvent.click(screen.getByRole("button", { name: "Cancel selected" }));
      fireEvent.click(await screen.findByRole("button", { name: "Cancel 3 reservations" }));

      await waitFor(() =>
        expect(toastError).toHaveBeenCalledWith("Cancelled 2, failed 1: Reservation not found"),
      );
      expect(toastSuccess).not.toHaveBeenCalled();
      expect(rowBox(2)).toBeChecked();
      expect(rowBox(1)).not.toBeChecked();
      expect(rowBox(3)).not.toBeChecked();
      expect(screen.getByRole("status", { name: "Selection" })).toHaveTextContent("1 selected");
      expect(invalidate).toHaveBeenCalledTimes(1);
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["reservations"] });
    });

    it("gives only the counts when the failures have different reasons", async () => {
      serveList([row(1, "ACTIVE"), row(2, "ACTIVE")]);
      server.use(
        http.put("/api/reservations/:id/release", ({ params }) =>
          HttpResponse.json(
            { detail: params.id === uuid(1) ? "one reason" : "another reason" },
            { status: 409 },
          ),
        ),
      );
      renderWithProviders(<ReservationsPage />);
      await screen.findByText("purpose 1");
      fireEvent.click(selectAllBox());
      fireEvent.click(screen.getByRole("button", { name: "Release selected" }));
      fireEvent.click(await screen.findByRole("button", { name: "Release 2 reservations" }));
      await waitFor(() => expect(toastError).toHaveBeenCalledWith("Released 0, failed 2"));
      expect(rowBox(1)).toBeChecked();
      expect(rowBox(2)).toBeChecked();
    });
  });
});
