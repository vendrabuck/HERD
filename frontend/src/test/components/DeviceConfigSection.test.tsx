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

  it("shows a create refusal's string detail and never renders a validation list", async () => {
    let calls = 0;
    server.use(
      http.get(VERSIONS_URL, () => HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 })),
      http.post(VERSIONS_URL, () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ detail: "config does not match the schema" }, { status: 422 })
          : HttpResponse.json(
              { detail: [{ loc: ["body", "config"], msg: "bad", type: "dict_type" }] },
              { status: 422 },
            );
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);
    await screen.findByText("No config versions yet.");
    await user.click(screen.getByRole("button", { name: "New version" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("config does not match the schema")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("Request failed with status code 422"),
    ).toBeInTheDocument();
  });

  describe("Apply dialog and Restore refusals (issue #1098)", () => {
    const VERSION_URL = `${VERSIONS_URL}/ver-1`;
    const GATE_MESSAGE =
      "This device's driver implements the Layer 3 Switch contract, which has no configure " +
      "method, so a config apply cannot run. Config versions on this device store intent only.";
    const GATE_DETAIL = {
      error: "driver_cannot_configure",
      connection_type: "Layer 3 Switch",
      driver: "frr_l3",
      message: GATE_MESSAGE,
    };
    const FORBIDDEN =
      "manage grant required on this device for an immediate apply " +
      "(a reservation owner can schedule the apply instead)";

    function oneVersion() {
      server.use(
        http.get(VERSIONS_URL, () =>
          HttpResponse.json({ items: [makeVersion()], total: 1, skip: 0, limit: 50 }),
        ),
      );
    }

    async function openApplyDialog() {
      const user = userEvent.setup();
      renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);
      await screen.findByText("v1");
      await user.click(screen.getByRole("button", { name: "Apply" }));
      // Every Modal titles itself through id="modal-title", so the section's
      // several modals share one id; find the dialog through its heading.
      const heading = await screen.findByRole("heading", { name: "Apply config to device" });
      const dialog = heading.closest("dialog") as HTMLElement;
      return { user, dialog };
    }

    async function confirmRestore() {
      const user = userEvent.setup();
      renderWithProviders(<DeviceConfigSection deviceId={DEVICE_ID} />);
      await screen.findByText("v1");
      const row = screen.getByText("v1").closest("tr") as HTMLElement;
      await user.click(within(row).getByRole("button", { name: "Restore" }));
      const heading = await screen.findByRole("heading", { name: "Restore this version?" });
      const dialog = heading.closest("dialog") as HTMLElement;
      await user.click(within(dialog).getByRole("button", { name: "Restore" }));
    }

    it("applies now and shows the run id on success", async () => {
      oneVersion();
      let applied = false;
      server.use(
        http.post(`${VERSION_URL}/apply`, () => {
          applied = true;
          return HttpResponse.json({
            version_id: "ver-1",
            run_id: "run-12345678-abcd",
            status: "success",
            error: null,
          });
        }),
      );
      const { user, dialog } = await openApplyDialog();
      await user.click(within(dialog).getByRole("button", { name: "Apply now" }));
      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Applied (run run-1234)"));
      expect(applied).toBe(true);
      expect(toastError).not.toHaveBeenCalled();
    });

    it("shows the stored error for a failed apply answer", async () => {
      oneVersion();
      server.use(
        http.post(`${VERSION_URL}/apply`, () =>
          HttpResponse.json({
            version_id: "ver-1",
            run_id: null,
            status: "failed",
            error: "execution service unreachable (ConnectError)",
          }),
        ),
      );
      const { user, dialog } = await openApplyDialog();
      await user.click(within(dialog).getByRole("button", { name: "Apply now" }));
      await waitFor(() =>
        expect(toastError).toHaveBeenCalledWith(
          "Apply failed: execution service unreachable (ConnectError)",
        ),
      );
    });

    it("shows the driver gate's sentence when Apply now is refused with 409", async () => {
      oneVersion();
      server.use(
        http.post(`${VERSION_URL}/apply`, () =>
          HttpResponse.json({ detail: GATE_DETAIL }, { status: 409 }),
        ),
      );
      const { user, dialog } = await openApplyDialog();
      await user.click(within(dialog).getByRole("button", { name: "Apply now" }));
      await waitFor(() =>
        expect(toastError).toHaveBeenCalledWith(GATE_MESSAGE + " (driver: frr_l3)"),
      );
      // A refusal keeps the dialog open so the user can schedule or cancel.
      const heading = screen.getByRole("heading", { name: "Apply config to device" });
      expect((heading.closest("dialog") as HTMLDialogElement).open).toBe(true);
    });

    it("shows the server's sentence when Apply now is refused with 403", async () => {
      oneVersion();
      server.use(
        http.post(`${VERSION_URL}/apply`, () =>
          HttpResponse.json({ detail: FORBIDDEN }, { status: 403 }),
        ),
      );
      const { user, dialog } = await openApplyDialog();
      await user.click(within(dialog).getByRole("button", { name: "Apply now" }));
      await waitFor(() => expect(toastError).toHaveBeenCalledWith(FORBIDDEN));
    });

    it("schedules at the chosen time and sends it as ISO", async () => {
      oneVersion();
      let body: { scheduled_for?: string } | undefined;
      server.use(
        http.post(`${VERSION_URL}/schedule`, async ({ request }) => {
          body = (await request.json()) as typeof body;
          return HttpResponse.json({ id: "job-1" });
        }),
      );
      const { user, dialog } = await openApplyDialog();
      await user.type(within(dialog).getByLabelText("Schedule for (optional)"), "2030-01-02T03:04");
      await user.click(within(dialog).getByRole("button", { name: "Schedule" }));
      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Scheduled"));
      expect(body?.scheduled_for).toBe(new Date("2030-01-02T03:04").toISOString());
    });

    it("shows the driver gate's sentence, not an object, when a schedule is refused", async () => {
      oneVersion();
      server.use(
        http.post(`${VERSION_URL}/schedule`, () =>
          HttpResponse.json({ detail: GATE_DETAIL }, { status: 409 }),
        ),
      );
      const { user, dialog } = await openApplyDialog();
      await user.type(within(dialog).getByLabelText("Schedule for (optional)"), "2030-01-02T03:04");
      await user.click(within(dialog).getByRole("button", { name: "Schedule" }));
      await waitFor(() =>
        expect(toastError).toHaveBeenCalledWith(GATE_MESSAGE + " (driver: frr_l3)"),
      );
      for (const call of toastError.mock.calls) {
        expect(typeof call[0]).toBe("string");
      }
    });

    it("shows a plain-string schedule refusal as given", async () => {
      oneVersion();
      server.use(
        http.post(`${VERSION_URL}/schedule`, () =>
          HttpResponse.json({ detail: "reservations service unreachable" }, { status: 503 }),
        ),
      );
      const { user, dialog } = await openApplyDialog();
      await user.type(within(dialog).getByLabelText("Schedule for (optional)"), "2030-01-02T03:04");
      await user.click(within(dialog).getByRole("button", { name: "Schedule" }));
      await waitFor(() =>
        expect(toastError).toHaveBeenCalledWith("reservations service unreachable"),
      );
    });

    it("lists the blocking reservations when a restore is refused with 409", async () => {
      oneVersion();
      server.use(
        http.post(`${VERSION_URL}/restore`, () =>
          HttpResponse.json(
            {
              detail: {
                message: "Device has active reservations; restore blocked",
                reservations: [
                  { id: "aaaa1111-0000-0000-0000-000000000000", status: "ACTIVE", end_time: null },
                  { id: "bbbb2222-0000-0000-0000-000000000000", status: "ACTIVE", end_time: null },
                ],
              },
            },
            { status: 409 },
          ),
        ),
      );
      await confirmRestore();
      await waitFor(() =>
        expect(toastError).toHaveBeenCalledWith(
          "Device has active reservations; restore blocked: aaaa1111 (ACTIVE), bbbb2222 (ACTIVE)",
        ),
      );
    });

    it("shows the guard's sentence when a restore fails closed with 503", async () => {
      oneVersion();
      const unreachable = "reservations service unreachable while checking active reservations";
      server.use(
        http.post(`${VERSION_URL}/restore`, () =>
          HttpResponse.json({ detail: unreachable }, { status: 503 }),
        ),
      );
      await confirmRestore();
      await waitFor(() => expect(toastError).toHaveBeenCalledWith(unreachable));
    });

    it("restores and shows a success toast", async () => {
      oneVersion();
      server.use(
        http.post(`${VERSION_URL}/restore`, () =>
          HttpResponse.json({ ...makeVersion({ id: "ver-2", version_number: 2 }), config: {} }),
        ),
      );
      await confirmRestore();
      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Restored as a new version"));
    });
  });
});
