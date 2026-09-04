import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

// Lab purpose classification, reporting section (issue #646 phase 1). A
// sibling file to ReportingPage.test.tsx, isolated so the by_purpose /
// by_user_purpose / by_device_purpose fixtures do not have to thread through
// every existing case in that file.

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

function authSnapshot() {
  return {
    user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
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

function mockDevices() {
  server.use(
    http.get("/api/inventory/devices", () =>
      HttpResponse.json({
        items: [
          { id: "dev-1", name: "fw-edge-01", template_name: "FW-3200" },
          { id: "dev-2", name: "fw-edge-02", template_name: "FW-3200" },
        ],
        total: 2,
        skip: 0,
        limit: 500,
      }),
    ),
  );
}

const BASE_REPORT = {
  window_start: "2026-05-01T00:00:00Z",
  window_end: "2026-05-31T00:00:00Z",
  total_hours: 42.5,
  total_reservations: 7,
  execution_run_count: 3,
  by_user: [{ user_id: "user-aaaa1111", owner_name: "alice", reservation_count: 4, hours: 30.0 }],
  by_device: [{ device_id: "dev-1", reservation_count: 5, hours: 25.0 }],
  by_topology_type: [{ topology_type: "PHYSICAL", reservation_count: 6, hours: 40.0 }],
  by_day: [{ day: "2026-05-15", reservation_count: 2, hours: 8.0 }],
  by_group: [{ group_id: "grp-1", group_name: "Platform Eng", reservation_count: 7, hours: 42.5 }],
  fleet: null,
};

const REPORT_WITH_PURPOSE = {
  ...BASE_REPORT,
  by_purpose: [
    { purpose_category: "qa_regression", reservations: 4, device_hours: 30.0 },
    { purpose_category: "unclassified", reservations: 3, device_hours: 12.5 },
  ],
  by_user_purpose: [
    { user_id: "user-aaaa1111", purpose_category: "qa_regression", reservations: 4, device_hours: 30.0 },
    { user_id: "user-bbbb2222", purpose_category: "unclassified", reservations: 3, device_hours: 12.5 },
  ],
  by_device_purpose: [
    { device_id: "dev-1", purpose_category: "qa_regression", reservations: 5, device_hours: 25.0 },
    { device_id: "dev-2", purpose_category: "unclassified", reservations: 2, device_hours: 17.5 },
  ],
};

beforeEach(() => {
  mockDevices();
});

describe("ReportingPage Purpose section", () => {
  it("renders the bar chart and both mix tables, including the unclassified bucket", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT_WITH_PURPOSE),
      ),
    );
    renderWithProviders(<ReportingPage />);

    await screen.findByText("Device-hours by purpose");
    // Both labels appear in the bar chart plus the two mix tables' tags below
    // it, so assert on the collection rather than a unique match.
    expect(screen.getAllByText("QA and regression").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Unclassified").length).toBeGreaterThan(0);

    const userMixHeading = screen.getByText("Purpose Mix - By User");
    const userMixCard = userMixHeading.closest("div")?.parentElement as HTMLElement;
    expect(within(userMixCard).getByText("alice")).toBeInTheDocument();
    // The second user row has no owner_name in the fixture; falls back to a
    // truncated id, matching the existing By User table's convention.
    expect(within(userMixCard).getByText("user-bbb")).toBeInTheDocument();

    const deviceMixHeading = screen.getByText("Purpose Mix - By Device");
    const deviceMixCard = deviceMixHeading.closest("div")?.parentElement as HTMLElement;
    await waitFor(() => expect(within(deviceMixCard).getByText("fw-edge-01")).toBeInTheDocument());
    expect(within(deviceMixCard).getByText("fw-edge-02")).toBeInTheDocument();
  });

  it("hides the whole section cleanly when by_purpose is absent (older backend)", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () => HttpResponse.json(BASE_REPORT)),
    );
    renderWithProviders(<ReportingPage />);

    // Wait for the report to actually land before asserting an absence.
    await screen.findByText("Platform Eng");
    expect(screen.queryByText("Device-hours by purpose")).not.toBeInTheDocument();
    expect(screen.queryByText("Purpose Mix - By User")).not.toBeInTheDocument();
    expect(screen.queryByText("Purpose Mix - By Device")).not.toBeInTheDocument();
  });

  it("shows a Show all toggle past 10 rows and reveals the rest", async () => {
    const manyUserRows = Array.from({ length: 12 }, (_, i) => ({
      user_id: `user-${i}`,
      purpose_category: "qa_regression",
      reservations: 1,
      device_hours: 12 - i, // strictly descending, so row order is deterministic
    }));
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json({
          ...REPORT_WITH_PURPOSE,
          by_user_purpose: manyUserRows,
        }),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const userMixHeading = await screen.findByText("Purpose Mix - By User");
    const userMixCard = userMixHeading.closest("div")?.parentElement as HTMLElement;

    await waitFor(() => expect(within(userMixCard).getByText("user-0")).toBeInTheDocument());
    expect(within(userMixCard).queryByText("user-10")).not.toBeInTheDocument();
    expect(within(userMixCard).queryByText("user-11")).not.toBeInTheDocument();

    fireEvent.click(within(userMixCard).getByRole("button", { name: "Show all (12)" }));
    expect(within(userMixCard).getByText("user-10")).toBeInTheDocument();
    expect(within(userMixCard).getByText("user-11")).toBeInTheDocument();

    fireEvent.click(within(userMixCard).getByRole("button", { name: "Show top 10" }));
    expect(within(userMixCard).queryByText("user-10")).not.toBeInTheDocument();
  });
});

// AI-suggested bucket in the purpose bar chart (issue #646 phase 2, ADR 0013
// point 9). by_purpose_suggested rows are separate from by_purpose: same
// category strings can appear in both, since one is confirmed rows and the
// other is unconfirmed-but-suggested rows.
describe("ReportingPage Purpose section, AI-suggested bucket", () => {
  const REPORT_WITH_SUGGESTED = {
    ...REPORT_WITH_PURPOSE,
    by_purpose_suggested: [
      { purpose_category: "qa_regression", reservations: 2, device_hours: 6.0 },
      { purpose_category: "customer_demo_poc", reservations: 1, device_hours: 3.0 },
    ],
  };

  it("renders suggested rows as additional, distinctly labeled bars with a legend", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT_WITH_SUGGESTED),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const chartHeading = await screen.findByText("Device-hours by purpose");
    const chartHeader = chartHeading.closest("div") as HTMLElement;
    expect(screen.getByText("QA and regression (suggested)")).toBeInTheDocument();
    expect(screen.getByText("Customer demo or POC (suggested)")).toBeInTheDocument();
    // The confirmed row for the same category string still renders on its
    // own, undisturbed, under its plain (non-suggested) label.
    expect(screen.getAllByText("QA and regression").length).toBeGreaterThan(0);

    // Legend: three distinct classes are named, not left to color alone.
    // Scoped to the chart's own header (which holds the legend) since
    // "Unclassified" also appears as a bar-row label and a mix-table tag.
    expect(within(chartHeader).getByText("Confirmed")).toBeInTheDocument();
    expect(within(chartHeader).getByText("AI-suggested")).toBeInTheDocument();
    expect(within(chartHeader).getByText("Unclassified")).toBeInTheDocument();
  });

  it("omits the AI-suggested legend entry when there are no suggested rows", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT_WITH_PURPOSE),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const chartHeading = await screen.findByText("Device-hours by purpose");
    const chartHeader = chartHeading.closest("div") as HTMLElement;
    expect(within(chartHeader).getByText("Confirmed")).toBeInTheDocument();
    expect(within(chartHeader).getByText("Unclassified")).toBeInTheDocument();
    expect(within(chartHeader).queryByText("AI-suggested")).not.toBeInTheDocument();
    expect(screen.queryByText(/\(suggested\)/)).not.toBeInTheDocument();
  });

  it("stays gated on by_purpose alone: an older backend with only by_purpose_suggested still hides", async () => {
    const { by_purpose: _omit, by_user_purpose: _omit2, by_device_purpose: _omit3, ...withoutConfirmed } =
      REPORT_WITH_SUGGESTED;
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(withoutConfirmed),
      ),
    );
    renderWithProviders(<ReportingPage />);

    await screen.findByText("Platform Eng");
    expect(screen.queryByText("Device-hours by purpose")).not.toBeInTheDocument();
  });
});

// Transit-gear inheritance in the by-device purpose mix (issue #646 phase 3,
// ADR 0013 "Delivery phases" point 3). dev-1 is transit-only (transit_device_hours
// equals device_hours); dev-2 is mixed (some reserved, some transit).
describe("ReportingPage Purpose section, transit-gear inheritance", () => {
  const REPORT_WITH_TRANSIT = {
    ...REPORT_WITH_PURPOSE,
    transit_included: true,
    by_device_purpose: [
      {
        device_id: "dev-1",
        purpose_category: "qa_regression",
        reservations: 3,
        device_hours: 12.0,
        transit_reservations: 3,
        transit_device_hours: 12.0,
      },
      {
        device_id: "dev-2",
        purpose_category: "unclassified",
        reservations: 4,
        device_hours: 20.0,
        transit_reservations: 1,
        transit_device_hours: 5.0,
      },
    ],
  };

  it("shows the Transit column, the mixed-row percentage, and the transit-only tag", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT_WITH_TRANSIT),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const deviceMixHeading = await screen.findByText("Purpose Mix - By Device");
    const deviceMixCard = deviceMixHeading.closest("div")?.parentElement as HTMLElement;

    await waitFor(() => expect(within(deviceMixCard).getByText("fw-edge-01")).toBeInTheDocument());
    expect(within(deviceMixCard).getByText("Transit")).toBeInTheDocument();

    // dev-1 (fw-edge-01): transit-only, transit_device_hours == device_hours,
    // so it carries the "transit" tag; Hours (12.0) and the Transit column's
    // hours (also 12.0) both render on this row, alongside the 100% share.
    const dev1Row = within(deviceMixCard).getByText("fw-edge-01").closest("tr") as HTMLElement;
    expect(within(dev1Row).getByText("transit")).toBeInTheDocument();
    expect(within(dev1Row).getAllByText("12.0")).toHaveLength(2);
    expect(within(dev1Row).getByText("(100%)")).toBeInTheDocument();

    // dev-2 (fw-edge-02): mixed, 5 of 20 hours transit = 25%.
    const dev2Row = within(deviceMixCard).getByText("fw-edge-02").closest("tr") as HTMLElement;
    expect(within(dev2Row).queryByText("transit")).not.toBeInTheDocument();
    expect(within(dev2Row).getByText("(25%)")).toBeInTheDocument();

    // Helper text when transit_included is true.
    expect(
      within(deviceMixCard).getByText("Includes transit gear on the reservation's paths"),
    ).toBeInTheDocument();
  });

  it("shows the reserved-devices-only helper text when transit_included is false", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json({ ...REPORT_WITH_TRANSIT, transit_included: false }),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const deviceMixHeading = await screen.findByText("Purpose Mix - By Device");
    const deviceMixCard = deviceMixHeading.closest("div")?.parentElement as HTMLElement;
    await waitFor(() => expect(within(deviceMixCard).getByText("fw-edge-01")).toBeInTheDocument());
    expect(within(deviceMixCard).getByText("Reserved devices only")).toBeInTheDocument();
  });

  it("renders cleanly, with no crash and no NaN, when a backend predates the transit fields", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT_WITH_PURPOSE),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const deviceMixHeading = await screen.findByText("Purpose Mix - By Device");
    const deviceMixCard = deviceMixHeading.closest("div")?.parentElement as HTMLElement;
    await waitFor(() => expect(within(deviceMixCard).getByText("fw-edge-01")).toBeInTheDocument());

    // The Transit column still renders (showTransitColumn is unconditional on
    // the by-device table) but every cell reads 0.0, with no percentage, no
    // tag, and no helper line since transit_included is absent.
    expect(within(deviceMixCard).getAllByText("0.0").length).toBeGreaterThan(0);
    expect(deviceMixCard.textContent).not.toMatch(/NaN/);
    expect(within(deviceMixCard).queryByText("transit")).not.toBeInTheDocument();
    expect(
      within(deviceMixCard).queryByText("Includes transit gear on the reservation's paths"),
    ).not.toBeInTheDocument();
    expect(within(deviceMixCard).queryByText("Reserved devices only")).not.toBeInTheDocument();
  });
});
