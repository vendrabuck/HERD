import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

beforeAll(() => {
  // jsdom has no real dialog. Toggle the open attribute so descendants stay in
  // the accessibility tree; a dialog without open is hidden and getByRole fails.
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

import { server } from "../mocks/server";
import { EditDevicesModal } from "@/components/reservations/EditDevicesModal";
import type { Reservation } from "@/types/reservation.types";

const RESERVATION: Reservation = {
  id: "11111111-2222-3333-4444-555555555555",
  user_id: "user-1",
  owner_name: "alice",
  device_ids: ["d-1", "d-2"],
  topology_id: "topo-1",
  topology_type: "PHYSICAL",
  purpose: "fw test",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-30T00:00:00Z",
};

// useAllDeviceNames walks /inventory/devices with no filters (limit 500); the
// modal also calls usePaginatedDevices with status/topology filters (limit 50).
// Both hit the same path, so handlers branch on the limit query param.
function makeDevice(id: string, name: string, templateName: string) {
  return {
    id,
    name,
    template_id: "tpl-1",
    template_name: templateName,
    template_icon: null,
    template_vendor: null,
    template_model: null,
    template_part_number: null,
    topology_type: "PHYSICAL",
    status: "AVAILABLE",
    field_data: {},
    exclusive: false,
    driver_id: null,
    driver_name: null,
    connection_type: null,
    created_at: "2026-05-30T00:00:00Z",
    updated_at: "2026-05-30T00:00:00Z",
    created_by: null,
    created_by_name: null,
    modified_by: null,
    modified_by_name: null,
    poll_interval_seconds: null,
    resolved_poll_interval_seconds: null,
  };
}

const NAME_MAP = [makeDevice("d-1", "router-alpha", "tpl"), makeDevice("d-2", "router-beta", "tpl")];

const AVAILABLE = [
  makeDevice("d-3", "switch-gamma", "Cisco 9300"),
  makeDevice("d-1", "router-alpha", "tpl"),
];

function devicesHandler(available = AVAILABLE) {
  return http.get("/api/inventory/devices", ({ request }) => {
    const url = new URL(request.url);
    const limit = url.searchParams.get("limit");
    // limit=500 is the all-names walk; limit=50 is the available-devices query.
    if (limit === "500") {
      return HttpResponse.json({ items: NAME_MAP, total: NAME_MAP.length, skip: 0, limit: 500 });
    }
    return HttpResponse.json({ items: available, total: available.length, skip: 0, limit: 50 });
  });
}

function renderModal(overrides: Partial<{ onClose: () => void; onUpdated: () => void }> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onClose = overrides.onClose ?? vi.fn();
  const onUpdated = overrides.onUpdated ?? vi.fn();
  const utils = render(
    <ProvidersWrapper client={client}>
      <EditDevicesModal
        reservation={RESERVATION}
        open={true}
        onClose={onClose}
        onUpdated={onUpdated}
      />
    </ProvidersWrapper>,
  );
  return { ...utils, onClose, onUpdated };
}

function ProvidersWrapper({ client, children }: { client: QueryClient; children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
  server.use(devicesHandler());
});

describe("EditDevicesModal", () => {
  it("renders the selected devices with their resolved names and count", async () => {
    renderModal();
    expect(screen.getByText("Edit Reservation Devices")).toBeInTheDocument();
    expect(screen.getByText("Selected Devices (2)")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("router-alpha")).toBeInTheDocument());
    expect(screen.getByText("router-beta")).toBeInTheDocument();
  });

  it("lists available devices excluding ones already selected", async () => {
    renderModal();
    // d-3 is available and not selected, so it shows; d-1 is available but
    // already selected, so it is filtered out of the add list.
    await waitFor(() => expect(screen.getByText("switch-gamma")).toBeInTheDocument());
    expect(screen.getByText("Cisco 9300")).toBeInTheDocument();
    expect(screen.queryByText("Add")).toBeInTheDocument();
    // d-1's row in the selected list exists, but it is not offered as an add row.
    expect(screen.getAllByText("router-alpha")).toHaveLength(1);
  });

  it("shows an empty state when no available devices are returned", async () => {
    server.use(devicesHandler([]));
    renderModal();
    await waitFor(() =>
      expect(screen.getByText("No available devices found")).toBeInTheDocument(),
    );
  });

  it("disables Save until the selection changes", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText("router-alpha")).toBeInTheDocument());
    const save = screen.getByRole("button", { name: "Save Changes" });
    expect(save).toBeDisabled();

    // Removing a selected device is a change, so Save enables.
    const removeButtons = screen.getAllByRole("button", { name: "Remove" });
    fireEvent.click(removeButtons[0]);
    expect(screen.getByText("Selected Devices (1)")).toBeInTheDocument();
    expect(save).toBeEnabled();
  });

  it("adds an available device to the selection on click", async () => {
    renderModal();
    const addRow = await screen.findByText("switch-gamma");
    fireEvent.click(addRow);
    expect(screen.getByText("Selected Devices (3)")).toBeInTheDocument();
    // Once selected, it is removed from the add list.
    await waitFor(() => expect(screen.queryByText("Cisco 9300")).not.toBeInTheDocument());
  });

  it("blocks saving an empty selection with an error toast", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText("router-alpha")).toBeInTheDocument());
    const removeButtons = screen.getAllByRole("button", { name: "Remove" });
    fireEvent.click(removeButtons[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    expect(screen.getByText("Selected Devices (0)")).toBeInTheDocument();
    expect(screen.getByText("No devices selected")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));
    expect(toastError).toHaveBeenCalledWith("At least one device must be selected");
  });

  it("patches the reservation and notifies on a successful save", async () => {
    let patchedBody: { device_ids?: string[] } | null = null;
    server.use(
      http.patch("/api/reservations/:id", async ({ request }) => {
        patchedBody = (await request.json()) as { device_ids?: string[] };
        return HttpResponse.json({ ...RESERVATION, device_ids: patchedBody.device_ids });
      }),
    );
    const { onClose, onUpdated } = renderModal();
    await waitFor(() => expect(screen.getByText("router-alpha")).toBeInTheDocument());

    fireEvent.click(await screen.findByText("switch-gamma"));
    expect(screen.getByText("Selected Devices (3)")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Reservation devices updated"),
    );
    const sent = patchedBody as { device_ids?: string[] } | null;
    expect(sent?.device_ids).toEqual(expect.arrayContaining(["d-1", "d-2", "d-3"]));
    expect(onUpdated).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("surfaces a server error as a toast without closing", async () => {
    server.use(
      http.patch("/api/reservations/:id", () =>
        HttpResponse.json({ detail: "conflict" }, { status: 409 }),
      ),
    );
    const { onClose, onUpdated } = renderModal();
    await waitFor(() => expect(screen.getByText("router-alpha")).toBeInTheDocument());

    fireEvent.click(await screen.findByText("switch-gamma"));
    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));

    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(onUpdated).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
