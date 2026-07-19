import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

const mockUpdateMutateAsync = vi.fn();
const mockDeleteMutateAsync = vi.fn();
const mockUseTemplate = vi.fn();

vi.mock("@/api/templates", () => ({
  useTemplate: (...args: unknown[]) => mockUseTemplate(...args),
}));

vi.mock("@/api/inventory", async () => {
  // Issue #391: keep the real deleteDeviceErrorMessage so the 409
  // device_in_use shape is exercised through the component's actual error
  // path, not a re-implementation in the test.
  const actual = await vi.importActual<typeof import("@/api/inventory")>("@/api/inventory");
  return {
    ...actual,
    useUpdateDevice: () => ({
      mutateAsync: mockUpdateMutateAsync,
      isPending: false,
    }),
    useDeleteDevice: () => ({
      mutateAsync: mockDeleteMutateAsync,
      isPending: false,
    }),
  };
});

// PortsSection has its own data hooks and tests; stub it so this file stays
// focused on the modal's own render, edit, and delete behavior.
vi.mock("@/components/devices/PortsSection", () => ({
  PortsSection: ({ deviceId }: { deviceId: string }) => (
    <div data-testid="ports-section">ports for {deviceId}</div>
  ),
}));

const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
  },
}));

import { DeviceDetailModal } from "@/components/devices/DeviceDetailModal";
import type { Device } from "@/types/device.types";

const BASE_DEVICE: Device = {
  id: "device-1",
  name: "fw-01",
  template_id: "tmpl-1",
  template_name: "FW-3200",
  template_icon: null,
  template_vendor: "Generic",
  template_model: "FW-3200",
  template_part_number: null,
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
  field_data: { serial: "ABC123" },
  exclusive: true,
  driver_id: null,
  driver_name: null,
  connection_type: null,
  created_at: "2026-01-15T00:00:00Z",
  updated_at: "2026-02-20T00:00:00Z",
  created_by: "user-1",
  created_by_name: "alice",
  modified_by: "user-2",
  modified_by_name: "bob",
  poll_interval_seconds: null,
  resolved_poll_interval_seconds: null,
};

beforeAll(() => {
  // jsdom does not implement <dialog>; the native methods leave the element's
  // open state untouched, which keeps its contents out of the accessibility
  // tree and breaks getByRole/getByText. Toggle `open` so queries see content.
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

beforeEach(() => {
  vi.clearAllMocks();
  mockUseTemplate.mockReturnValue({ data: undefined });
});

describe("DeviceDetailModal", () => {
  it("renders no device body when device is null", () => {
    render(<DeviceDetailModal device={null} onClose={vi.fn()} isAdmin={false} />);
    // The Modal shell mounts regardless, but with no device there is no body:
    // no summary heading, no ports section, no Close action.
    expect(screen.queryByTestId("ports-section")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
  });

  it("renders the device summary in read-only view", () => {
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={false} />);
    expect(screen.getByText("Device Details")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: "fw-01" }),
    ).toBeInTheDocument();
    expect(screen.getByText("FW-3200")).toBeInTheDocument();
    expect(screen.getByText("PHYSICAL")).toBeInTheDocument();
    expect(screen.getByText("AVAILABLE")).toBeInTheDocument();
    expect(screen.getByTestId("ports-section")).toHaveTextContent("device-1");
  });

  it("hides admin actions for non-admin users", () => {
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={false} />);
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("shows Edit and Delete actions for admin users", () => {
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={true} />);
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("renders the driver row only when a driver is attached", () => {
    const { rerender } = render(
      <DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={false} />,
    );
    expect(screen.queryByText("Driver:")).not.toBeInTheDocument();

    rerender(
      <DeviceDetailModal
        device={{ ...BASE_DEVICE, driver_name: "mgmt-driver" }}
        onClose={vi.fn()}
        isAdmin={false}
      />,
    );
    expect(screen.getByText("Driver:")).toBeInTheDocument();
    expect(screen.getByText("mgmt-driver")).toBeInTheDocument();
  });

  it("switches into edit mode and prefills the name field", () => {
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={true} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    expect(screen.getByText("Edit Device")).toBeInTheDocument();
    const nameInput = screen.getByLabelText("Name") as HTMLInputElement;
    expect(nameInput.value).toBe("fw-01");
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("saves an edited device and surfaces a success toast", async () => {
    mockUpdateMutateAsync.mockResolvedValue({});
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={true} />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "fw-01-renamed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockUpdateMutateAsync).toHaveBeenCalledTimes(1));
    expect(mockUpdateMutateAsync).toHaveBeenCalledWith({
      id: "device-1",
      data: {
        name: "fw-01-renamed",
        topology_type: "PHYSICAL",
        status: "AVAILABLE",
        field_data: { serial: "ABC123" },
      },
    });
    expect(mockToastSuccess).toHaveBeenCalledWith("Device updated");
    await waitFor(() =>
      expect(screen.getByText("Device Details")).toBeInTheDocument(),
    );
  });

  it("surfaces the backend detail message when an update fails", async () => {
    mockUpdateMutateAsync.mockRejectedValue({
      response: { data: { detail: "name already taken" } },
    });
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={true} />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("name already taken"),
    );
    // Stays in edit mode on failure.
    expect(screen.getByText("Edit Device")).toBeInTheDocument();
  });

  it("opens the delete confirmation and calls onClose after a successful delete", async () => {
    mockDeleteMutateAsync.mockResolvedValue({});
    const onClose = vi.fn();
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={onClose} isAdmin={true} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByText("Delete Device")).toBeInTheDocument();

    // The confirm dialog's destructive action carries the "Delete" label too;
    // grab the button inside the open confirm dialog.
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    const confirmButton = deleteButtons[deleteButtons.length - 1] as HTMLButtonElement;
    fireEvent.click(confirmButton);

    await waitFor(() =>
      expect(mockDeleteMutateAsync).toHaveBeenCalledWith("device-1"),
    );
    expect(mockToastSuccess).toHaveBeenCalledWith("Device deleted");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("toasts a readable message when delete is blocked by an active reservation (409)", async () => {
    // Issue #391: inventory's delete guard 409s with a structured
    // {error, reservation_ids} detail, not a plain string; the modal must not
    // toast the raw object.
    mockDeleteMutateAsync.mockRejectedValue({
      response: {
        data: {
          detail: { error: "device_in_use", reservation_ids: ["r1", "r2"] },
        },
      },
    });
    const onClose = vi.fn();
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={onClose} isAdmin={true} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    fireEvent.click(deleteButtons[deleteButtons.length - 1] as HTMLButtonElement);

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith(
        "Device is held by an active reservation and cannot be deleted",
      ),
    );
    // The modal must not close or report success on a blocked delete.
    expect(onClose).not.toHaveBeenCalled();
    expect(mockToastSuccess).not.toHaveBeenCalledWith("Device deleted");
  });

  it("renders dynamic template fields read-only when the template has sections", () => {
    mockUseTemplate.mockReturnValue({
      data: {
        sections: [
          {
            name: "Identity",
            fields: [
              { key: "serial", label: "Serial", type: "string" },
            ],
          },
        ],
      },
    });
    render(<DeviceDetailModal device={BASE_DEVICE} onClose={vi.fn()} isAdmin={false} />);
    expect(screen.getByText("Identity")).toBeInTheDocument();
    const serialInput = screen.getByLabelText("Serial") as HTMLInputElement;
    expect(serialInput.value).toBe("ABC123");
    expect(serialInput).toBeDisabled();
  });
});
