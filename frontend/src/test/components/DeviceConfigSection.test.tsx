import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { http, HttpResponse } from "msw";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { server } from "../mocks/server";

// The scheduled-applies panel has its own data hooks and tests; stub it so this
// file exercises only the version-history surface of DeviceConfigSection.
vi.mock("@/components/device-config/ApplyJobsPanel", () => ({
  ApplyJobsPanel: () => <div data-testid="apply-jobs-panel" />,
}));

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

import { DeviceConfigSection } from "@/components/device-config/DeviceConfigSection";
import type { DeviceConfigVersion } from "@/api/deviceConfig";

const DEVICE_ID = "device-1";
const VERSIONS_URL = `/api/inventory/devices/${DEVICE_ID}/config-versions`;

function makeVersion(overrides: Partial<DeviceConfigVersion> = {}): DeviceConfigVersion {
  return {
    id: "ver-1",
    device_id: DEVICE_ID,
    version_number: 1,
    connection_type: "ssh",
    description: "initial",
    created_by: "abcdef0123456789",
    author_name: "alice",
    created_at: "2026-02-20T12:00:00Z",
    restored_from_id: null,
    last_apply_run_id: null,
    ...overrides,
  };
}

function renderWithProviders(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("DeviceConfigSection", () => {
  beforeEach(() => {
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  it("shows the empty state when the device has no config versions", async () => {
    server.use(
      http.get(VERSIONS_URL, () => HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 })),
    );

    renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);

    expect(await screen.findByText("No config versions yet.")).toBeInTheDocument();
    expect(screen.getByText("0 versions")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders an error message when the versions request fails", async () => {
    server.use(
      http.get(VERSIONS_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);

    expect(await screen.findByText("Failed to load versions")).toBeInTheDocument();
  });

  it("renders a populated version table with author and singular count", async () => {
    server.use(
      http.get(VERSIONS_URL, () =>
        HttpResponse.json({
          items: [makeVersion({ version_number: 3, author_name: "alice", description: "tuned" })],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );

    renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);

    expect(await screen.findByText("v3")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("tuned")).toBeInTheDocument();
    // total === 1 takes the singular branch.
    expect(screen.getByText("1 version")).toBeInTheDocument();
  });

  it("keeps Compare disabled until exactly two versions are selected", async () => {
    server.use(
      http.get(VERSIONS_URL, () =>
        HttpResponse.json({
          items: [
            makeVersion({ id: "ver-1", version_number: 1 }),
            makeVersion({ id: "ver-2", version_number: 2 }),
          ],
          total: 2,
          skip: 0,
          limit: 50,
        }),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);

    await screen.findByText("v1");
    const compareButton = screen.getByRole("button", { name: "Compare" });
    expect(compareButton).toBeDisabled();

    await user.click(screen.getByLabelText("Compare v1"));
    expect(compareButton).toBeDisabled();

    await user.click(screen.getByLabelText("Compare v2"));
    expect(compareButton).toBeEnabled();
  });

  it("surfaces a JSON parse error in the create modal without calling the API", async () => {
    let createCalled = false;
    server.use(
      http.get(VERSIONS_URL, () => HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 })),
      http.post(VERSIONS_URL, () => {
        createCalled = true;
        return HttpResponse.json(makeVersion());
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);

    await screen.findByText("No config versions yet.");
    await user.click(screen.getByRole("button", { name: "New version" }));

    const textarea = await screen.findByLabelText("Config (JSON)");
    await user.type(textarea, "not json");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // Invalid JSON short-circuits before the mutation fires.
    expect(createCalled).toBe(false);
    expect(toastSuccess).not.toHaveBeenCalled();
    // An error string from JSON.parse is rendered in the modal.
    await waitFor(() => {
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByText(/.+/, { selector: "p.text-red-600" })).toBeInTheDocument();
    });
  });

  it("creates a new version with parsed JSON and shows a success toast", async () => {
    let receivedBody: { config?: Record<string, unknown>; description?: string } | undefined;
    server.use(
      http.get(VERSIONS_URL, () => HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 })),
      http.post(VERSIONS_URL, async ({ request }) => {
        receivedBody = (await request.json()) as typeof receivedBody;
        return HttpResponse.json({ ...makeVersion(), config: { vlan: 100 } });
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);

    await screen.findByText("No config versions yet.");
    await user.click(screen.getByRole("button", { name: "New version" }));

    const textarea = await screen.findByLabelText("Config (JSON)");
    await user.type(textarea, '{{"vlan": 100}');
    await user.type(screen.getByLabelText("Description (optional)"), "first cut");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Config version created"));
    expect(receivedBody).toEqual({ config: { vlan: 100 }, description: "first cut" });
  });
});
