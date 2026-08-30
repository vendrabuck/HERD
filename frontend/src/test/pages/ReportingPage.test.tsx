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

// The page itself no longer reads the auth store (the admin gate now lives
// in the AdminGuard route group in routes.tsx; see AdminGuard.test.tsx), but the
// shared axios client still reaches for the static useAuthStore.getState()
// in its request interceptor to attach the bearer token, so an admin
// snapshot is still needed here.
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
  fleet: {
    device_count: 3,
    idle_device_count: 1,
    window_hours: 720.0,
    total_reserved_hours: 42.5,
    utilization_pct: 1.97,
    devices: [
      {
        device_id: "dev-fw-1",
        name: "fw-edge-01",
        status: "AVAILABLE",
        reservation_count: 5,
        hours: 25.0,
        utilization_pct: 3.5,
      },
      {
        device_id: "dev-fw-2",
        name: "fw-edge-02",
        status: "RESERVED",
        reservation_count: 2,
        hours: 17.5,
        utilization_pct: 2.4,
      },
      {
        device_id: "dev-sw-3",
        name: "sw-core-03",
        status: "MAINTENANCE",
        reservation_count: 0,
        hours: 0.0,
        utilization_pct: 0.0,
      },
    ],
  },
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
  mockDevices();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReportingPage", () => {
  // Non-admin redirect coverage moved to AdminGuard.test.tsx: the guard now
  // lives in the AdminGuard route group in routes.tsx (issue #527, issue #548),
  // and this page no longer performs its own redirect check.

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
    // The device name now renders in both the By Device table and the fleet
    // table, so assert on the collection rather than a unique match.
    await waitFor(() =>
      expect(screen.getAllByText("fw-edge-01").length).toBeGreaterThan(0),
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
    // Names appear in both the By Device table and the fleet table.
    expect(screen.getAllByText("fw-edge-01").length).toBeGreaterThan(0);
    expect(screen.getAllByText("fw-edge-02").length).toBeGreaterThan(0);

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

  it("renders fleet utilization stats and per-device rates", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const fleetHeading = await screen.findByText("Fleet Utilization");
    const fleetCard = fleetHeading.closest("div")?.parentElement as HTMLElement;

    // Summary stats: fleet-wide pct, device count, idle count.
    await waitFor(() =>
      expect(within(fleetCard).getByText("2.0%")).toBeInTheDocument(),
    );
    expect(within(fleetCard).getByText("Idle in window")).toBeInTheDocument();

    // Per-device rows carry name, current status, and the rate.
    expect(within(fleetCard).getByText("sw-core-03")).toBeInTheDocument();
    expect(within(fleetCard).getByText("MAINTENANCE")).toBeInTheDocument();
    expect(within(fleetCard).getByText("3.5%")).toBeInTheDocument();
    expect(within(fleetCard).getByText("0.0%")).toBeInTheDocument();
  });

  it("filters the fleet table to idle devices with the toggle", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json(REPORT),
      ),
    );
    renderWithProviders(<ReportingPage />);

    const fleetHeading = await screen.findByText("Fleet Utilization");
    const fleetCard = fleetHeading.closest("div")?.parentElement as HTMLElement;
    await waitFor(() =>
      expect(within(fleetCard).getByText("sw-core-03")).toBeInTheDocument(),
    );

    fireEvent.click(within(fleetCard).getByLabelText("Idle only"));

    // Only the zero-booking device survives the filter.
    expect(within(fleetCard).getByText("sw-core-03")).toBeInTheDocument();
    expect(within(fleetCard).queryByText("fw-edge-01")).not.toBeInTheDocument();
    expect(within(fleetCard).queryByText("fw-edge-02")).not.toBeInTheDocument();
  });

  it("shows an unavailable notice when the fleet section is null", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json({ ...REPORT, fleet: null }),
      ),
    );
    renderWithProviders(<ReportingPage />);
    await waitFor(() =>
      expect(
        screen.getByText(/fleet utilization is unavailable/i),
      ).toBeInTheDocument(),
    );
  });

  it("downloads a server-generated CSV when a table's Download CSV button is clicked", async () => {
    const requestedSections: string[] = [];
    server.use(
      http.get("/api/reservations/reports/utilization", () => HttpResponse.json(REPORT)),
      http.get("/api/reservations/reports/utilization.csv", ({ request }) => {
        const section = new URL(request.url).searchParams.get("section") ?? "";
        requestedSections.push(section);
        return new HttpResponse(`section,${section}\n`, {
          headers: {
            "content-type": "text/csv",
            "content-disposition": `attachment; filename="utilization-${section}.csv"`,
          },
        });
      }),
    );

    // jsdom has no Blob-URL support; patch the two methods triggerCsvDownload
    // calls directly on the real URL constructor (never replace URL itself,
    // since page code and the MSW handler above both still need `new URL()`).
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:mock-url");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    renderWithProviders(<ReportingPage />);

    const userHeading = await screen.findByText("By User");
    const userCard = userHeading.closest("div")?.parentElement as HTMLElement;
    // The button stays disabled (canDownload gate) until the report query
    // resolves; wait for that before clicking.
    const userDownloadButton = within(userCard).getByRole("button", { name: "Download CSV" });
    await waitFor(() => expect(userDownloadButton).toBeEnabled());
    fireEvent.click(userDownloadButton);

    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
    expect(requestedSections).toEqual(["user"]);

    // The By Device table's own CSV button requests the "device" section.
    const deviceHeading = screen.getByText("By Device");
    const deviceCard = deviceHeading.closest("div")?.parentElement as HTMLElement;
    fireEvent.click(within(deviceCard).getByRole("button", { name: "Download CSV" }));
    await waitFor(() => expect(requestedSections).toEqual(["user", "device"]));

    // The fleet card's CSV button requests the "fleet" section.
    const fleetHeading = screen.getByText("Fleet Utilization");
    const fleetCard = fleetHeading.closest("div")?.parentElement as HTMLElement;
    fireEvent.click(within(fleetCard).getByRole("button", { name: "Download CSV" }));
    await waitFor(() => expect(requestedSections).toEqual(["user", "device", "fleet"]));
  });

  it("downloads a client-built by-template CSV with escaped cells, sorted by hours desc", async () => {
    // A template name containing a comma exercises escapeCsvCell's quoting
    // branch; two distinct templates exercise the sort-by-hours comparator.
    const DEVICE_COMMA = { id: "dev-comma", name: "sw-comma", template_name: 'Acme, Inc "Switch"' };
    server.use(
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({
          items: [DEVICE_FW1, DEVICE_FW2, DEVICE_COMMA],
          total: 3,
          skip: 0,
          limit: 500,
        }),
      ),
      http.get("/api/reservations/reports/utilization", () =>
        HttpResponse.json({
          ...REPORT,
          by_device: [
            ...REPORT.by_device,
            { device_id: "dev-comma", reservation_count: 1, hours: 5.0 },
          ],
        }),
      ),
    );

    // Real Blob (MSW's own response handling relies on the global Blob, so it
    // must not be mocked); only the URL/anchor download plumbing is stubbed.
    // The Blob instance passed to createObjectURL is inspected via .text().
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock-url");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    renderWithProviders(<ReportingPage />);

    const templateHeading = await screen.findByText("By Template");
    const templateCard = templateHeading.closest("div")?.parentElement as HTMLElement;
    // Wait for both template rows to land before downloading.
    await within(templateCard).findByText('Acme, Inc "Switch"');
    const downloadButton = within(templateCard).getByRole("button", { name: "Download CSV" });
    await waitFor(() => expect(downloadButton).toBeEnabled());
    fireEvent.click(downloadButton);

    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    const blobArg = createObjectURL.mock.calls[0][0] as Blob;
    const capturedBody = await blobArg.text();
    expect(capturedBody).toContain("template_name,hours,reservation_count");
    // FW-3200's 42.5 hours outranks the comma template's 5.0, so it sorts first.
    const fwLine = capturedBody.split("\n").find((l) => l.startsWith("FW-3200"));
    const commaLine = capturedBody.split("\n").find((l) => l.startsWith('"Acme'));
    expect(fwLine).toBe("FW-3200,42.5000,7");
    // The comma and embedded quote are escaped per RFC 4180.
    expect(commaLine).toBe('"Acme, Inc ""Switch""",5.0000,1');
    expect(capturedBody.indexOf(fwLine!)).toBeLessThan(capturedBody.indexOf(commaLine!));
  });

  it("switches to the 7-day and 30-day presets, requesting a fresh window each time", async () => {
    const requestedRanges: string[] = [];
    server.use(
      http.get("/api/reservations/reports/utilization", ({ request }) => {
        const url = new URL(request.url);
        requestedRanges.push(`${url.searchParams.get("start")}|${url.searchParams.get("end")}`);
        return HttpResponse.json(REPORT);
      }),
    );
    renderWithProviders(<ReportingPage />);

    // Default preset is 30 days; wait for the initial load.
    await waitFor(() => expect(requestedRanges.length).toBeGreaterThan(0));
    const thirtyDayButton = screen.getByRole("button", { name: "30 days" });
    expect(thirtyDayButton.className).toContain("bg-gray-900");

    fireEvent.click(screen.getByRole("button", { name: "7 days" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "7 days" }).className).toContain("bg-gray-900"),
    );
    expect(thirtyDayButton.className).not.toContain("bg-gray-900");
    // A new, narrower window was requested for the 7-day preset.
    await waitFor(() => expect(requestedRanges.length).toBeGreaterThan(1));

    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "30 days" }).className).toContain("bg-gray-900"),
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
