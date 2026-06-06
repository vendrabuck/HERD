import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

// Auth role drives the admin gate on this page. Default to an admin; individual
// tests override the role to exercise the non-admin redirect branch.
let mockRole: "admin" | "superadmin" | "user" = "admin";
const mockNavigate = vi.fn();

// The page reads the store via the hook selector, while the shared axios client
// reaches for the static useAuthStore.getState() in its request interceptor to
// attach the bearer token. Both code paths must see the same admin snapshot, so
// the mock exposes a callable selector hook plus a getState method.
function authSnapshot() {
  return {
    user: { id: "1", role: mockRole, username: "admin", email: "a@b.c" },
    accessToken: "test-token",
    refreshToken: "test-refresh",
    setTokens: vi.fn(),
    clearAuth: vi.fn(),
  };
}

vi.mock("@/stores/authStore", () => {
  const useAuthStore = (sel: (s: unknown) => unknown) => sel(authSnapshot());
  useAuthStore.getState = () => authSnapshot();
  return { useAuthStore };
});

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

import { server } from "../mocks/server";
import { ReportingPage } from "@/pages/ReportingPage";

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

const DEVICE_FW1 = {
  id: "dev-fw-1",
  name: "fw-edge-01",
  template_name: "FW-3200",
};

const DEVICE_FW2 = {
  id: "dev-fw-2",
  name: "fw-edge-02",
  template_name: "FW-3200",
};

const REPORT = {
  window_start: "2026-05-01T00:00:00Z",
  window_end: "2026-05-31T00:00:00Z",
  total_hours: 42.5,
  total_reservations: 7,
  execution_run_count: 3,
  by_user: [
    { user_id: "user-aaaa1111", owner_name: "alice", reservation_count: 4, hours: 30.0 },
    { user_id: "user-bbbb2222", owner_name: "", reservation_count: 3, hours: 12.5 },
  ],
  by_device: [
    { device_id: "dev-fw-1", reservation_count: 5, hours: 25.0 },
    { device_id: "dev-fw-2", reservation_count: 2, hours: 17.5 },
  ],
  by_topology_type: [
    { topology_type: "PHYSICAL", reservation_count: 6, hours: 40.0 },
  ],
  by_day: [{ day: "2026-05-15", reservation_count: 2, hours: 8.0 }],
  by_group: [{ group_id: "grp-1", group_name: "Platform Eng", reservation_count: 7, hours: 42.5 }],
};

function mockDevices() {
  server.use(
    http.get("/api/inventory/devices", () =>
      HttpResponse.json({
        items: [DEVICE_FW1, DEVICE_FW2],
        total: 2,
        skip: 0,
        limit: 500,
      }),
    ),
  );
}

beforeEach(() => {
  mockRole = "admin";
  mockNavigate.mockReset();
  mockDevices();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReportingPage", () => {
  it("redirects a non-admin user away and renders nothing", async () => {
    mockRole = "user";
    const { container } = renderWithProviders(<ReportingPage />);
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/topology"));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the heading and range selector for an admin", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT),
      ),
    );
    renderWithProviders(<ReportingPage />);
    expect(
      screen.getByRole("heading", { name: "Utilization Report" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "30 days" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("fw-edge-01")).toBeInTheDocument(),
    );
  });

  it("populates the headline stats and the per-user and per-device tables", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT),
      ),
    );
    renderWithProviders(<ReportingPage />);

    // Each headline stat lives in its own card: a label div over a value div.
    // 42.5/7/3 also appear in the tables below, so scope each assertion to the
    // card by walking from the label to its sibling value rather than a bare
    // getByText, which would otherwise match multiple elements.
    const statValue = (label: string) =>
      screen.getByText(label).parentElement?.querySelector("div.text-3xl")
        ?.textContent;

    await waitFor(() =>
      expect(statValue("Total reservation-hours")).toBe("42.5"),
    );
    expect(statValue("Reservations counted")).toBe("7");
    expect(statValue("Execution runs")).toBe("3");

    // Named user shows owner_name; the second falls back to a sliced user_id.
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("user-bbb")).toBeInTheDocument();

    // Device rows resolve names through the device index from inventory.
    expect(screen.getByText("fw-edge-01")).toBeInTheDocument();
    expect(screen.getByText("fw-edge-02")).toBeInTheDocument();

    // Group and topology buckets render their labels.
    expect(screen.getByText("Platform Eng")).toBeInTheDocument();
    expect(screen.getByText("PHYSICAL")).toBeInTheDocument();
  });

  it("rolls up by-device hours into a by-template table", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT),
      ),
    );
    renderWithProviders(<ReportingPage />);

    // Both devices map to FW-3200; their hours (25.0 + 17.5) sum to 42.5.
    const templateHeading = await screen.findByText("By Template");
    const templateCard = templateHeading.closest("div")?.parentElement as HTMLElement;
    await waitFor(() =>
      expect(within(templateCard).getByText("FW-3200")).toBeInTheDocument(),
    );
    expect(within(templateCard).getByText("42.5")).toBeInTheDocument();
  });

  it("renders empty rows when the report has no buckets", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json({
          ...REPORT,
          total_hours: 0,
          total_reservations: 0,
          execution_run_count: 0,
          by_user: [],
          by_device: [],
          by_topology_type: [],
          by_day: [],
          by_group: [],
        }),
      ),
    );
    renderWithProviders(<ReportingPage />);
    await waitFor(() =>
      expect(screen.getAllByText("No data in this window").length).toBeGreaterThan(0),
    );
  });

  it("shows an error strip when the report request fails", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderWithProviders(<ReportingPage />);
    await waitFor(() =>
      expect(screen.getByText(/Failed to load report/i)).toBeInTheDocument(),
    );
  });

  it("warns when a custom range has an end before its start", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT),
      ),
    );
    renderWithProviders(<ReportingPage />);

    fireEvent.click(screen.getByRole("button", { name: "Custom" }));
    const dateInputs = screen.getAllByDisplayValue(/\d{4}-\d{2}-\d{2}/);
    // dateInputs[0] is start, dateInputs[1] is end. Set end before start.
    fireEvent.change(dateInputs[0], { target: { value: "2026-05-20" } });
    fireEvent.change(dateInputs[1], { target: { value: "2026-05-10" } });

    await waitFor(() =>
      expect(
        screen.getByText("End date must be after start date."),
      ).toBeInTheDocument(),
    );
  });
});
