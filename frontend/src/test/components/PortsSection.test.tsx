import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";

import type { Port } from "@/types/port.types";
import type { DeviceTemplate } from "@/types/template.types";

// Mutation spies are shared across tests so each case can assert on calls and
// drive success/failure of mutateAsync.
const mockCreatePort = vi.fn();
const mockCreatePortsBulk = vi.fn();
const mockDeletePort = vi.fn();

const mockUsePorts = vi.fn();
const mockUseTemplates = vi.fn();
const mockUseDeviceConnections = vi.fn();
const mockUseAllDeviceNames = vi.fn();

vi.mock("@/api/ports", () => ({
  usePorts: (...args: unknown[]) => mockUsePorts(...args),
  useCreatePort: () => ({ mutateAsync: mockCreatePort, isPending: false }),
  useCreatePortsBulk: () => ({ mutateAsync: mockCreatePortsBulk, isPending: false }),
  useDeletePort: () => ({ mutateAsync: mockDeletePort, isPending: false }),
}));

vi.mock("@/api/templates", () => ({
  useTemplates: (...args: unknown[]) => mockUseTemplates(...args),
}));

vi.mock("@/api/connections", () => ({
  useDeviceConnections: (...args: unknown[]) => mockUseDeviceConnections(...args),
}));

vi.mock("@/api/inventory", () => ({
  useAllDeviceNames: (...args: unknown[]) => mockUseAllDeviceNames(...args),
}));

// The dynamic field renderer is exercised by its own test; stub it so the port
// form's field section reduces to a stable marker we can assert on.
vi.mock("@/components/devices/DynamicFieldRenderer", () => ({
  DynamicFieldRenderer: () => <div data-testid="dynamic-fields" />,
}));

const mockToastError = vi.fn();
const mockToastSuccess = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: {
    error: (...args: unknown[]) => mockToastError(...args),
    success: (...args: unknown[]) => mockToastSuccess(...args),
  },
}));

import { PortsSection } from "@/components/devices/PortsSection";

const DEVICE_ID = "device-1";

const PORT_TEMPLATE: DeviceTemplate = {
  id: "tmpl-port-1",
  name: "Ethernet Port",
  category: "port",
  sections: [],
} as unknown as DeviceTemplate;

function makePort(overrides: Partial<Port> = {}): Port {
  return {
    id: "port-1",
    name: "GigE0/1",
    device_id: DEVICE_ID,
    template_id: PORT_TEMPLATE.id,
    template_name: "Ethernet Port",
    template_icon: null,
    field_data: { speed: "1G" },
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderWithProviders(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

beforeEach(() => {
  vi.clearAllMocks();
  // Sensible defaults; individual tests override usePorts as needed.
  mockUsePorts.mockReturnValue({ data: [], isLoading: false });
  mockUseTemplates.mockReturnValue({ data: [PORT_TEMPLATE] });
  mockUseDeviceConnections.mockReturnValue({ data: [] });
  mockUseAllDeviceNames.mockReturnValue({ data: new Map<string, string>() });
});

describe("PortsSection", () => {
  it("shows the loading state while ports are loading", () => {
    mockUsePorts.mockReturnValue({ data: undefined, isLoading: true });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} />);
    expect(screen.getByText("Loading ports...")).toBeInTheDocument();
  });

  it("shows the empty state when there are no ports", () => {
    mockUsePorts.mockReturnValue({ data: [], isLoading: false });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} />);
    expect(screen.getByText("No ports configured")).toBeInTheDocument();
  });

  it("renders a populated port table with name, template, and serialized fields", () => {
    mockUsePorts.mockReturnValue({ data: [makePort()], isLoading: false });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} />);
    // Scope to the table: the bulk-create modal is always mounted (its <dialog>
    // stays in the DOM under jsdom) and renders the template name as an option,
    // so an unscoped getByText("Ethernet Port") would match twice.
    const table = screen.getByRole("table");
    const tableQueries = within(table);
    expect(tableQueries.getByText("GigE0/1")).toBeInTheDocument();
    expect(tableQueries.getByText("Ethernet Port")).toBeInTheDocument();
    expect(tableQueries.getByText("speed: 1G")).toBeInTheDocument();
  });

  it("renders a connected-device link derived from the connection map", () => {
    mockUsePorts.mockReturnValue({ data: [makePort()], isLoading: false });
    mockUseDeviceConnections.mockReturnValue({
      data: [
        {
          id: "conn-1",
          device_a_id: DEVICE_ID,
          port_a: "GigE0/1",
          device_b_id: "device-2",
          port_b: "GigE0/2",
        },
      ],
    });
    mockUseAllDeviceNames.mockReturnValue({
      data: new Map([["device-2", "spine-01"]]),
    });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} />);
    const link = screen.getByRole("link", { name: "spine-01, GigE0/2" });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("href", "/inventory/device-2");
  });

  it("hides admin controls and the actions column when isAdmin is falsey", () => {
    mockUsePorts.mockReturnValue({ data: [makePort()], isLoading: false });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} />);
    expect(screen.queryByText("+ Add Port")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("validates the add-port form and surfaces an error when the name is blank", () => {
    mockUsePorts.mockReturnValue({ data: [], isLoading: false });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} isAdmin />);

    fireEvent.click(screen.getByRole("button", { name: "+ Add Port" }));
    // Form is open; submit without filling anything.
    fireEvent.click(screen.getByRole("button", { name: "Add Port" }));

    expect(mockToastError).toHaveBeenCalledWith("Port name is required");
    expect(mockCreatePort).not.toHaveBeenCalled();
  });

  it("submits a valid add-port form with the trimmed name and selected template", async () => {
    mockCreatePort.mockResolvedValue(makePort());
    mockUsePorts.mockReturnValue({ data: [], isLoading: false });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} isAdmin />);

    fireEvent.click(screen.getByRole("button", { name: "+ Add Port" }));
    fireEvent.change(screen.getByLabelText("Port name"), {
      target: { value: "  GigE0/3  " },
    });
    // The bulk modal (always mounted under jsdom) also has a "Port template"
    // label, so target the inline add-form select by its unique id instead.
    const addTemplateSelect = document.getElementById(
      "port-template",
    ) as HTMLSelectElement;
    fireEvent.change(addTemplateSelect, {
      target: { value: PORT_TEMPLATE.id },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add Port" }));

    await waitFor(() => expect(mockCreatePort).toHaveBeenCalledTimes(1));
    expect(mockCreatePort).toHaveBeenCalledWith({
      deviceId: DEVICE_ID,
      data: {
        name: "GigE0/3",
        template_id: PORT_TEMPLATE.id,
        field_data: {},
      },
    });
    expect(mockToastSuccess).toHaveBeenCalledWith("Port added");
  });

  it("deletes a port after confirmation and reports success", async () => {
    mockDeletePort.mockResolvedValue(undefined);
    mockUsePorts.mockReturnValue({ data: [makePort()], isLoading: false });
    renderWithProviders(<PortsSection deviceId={DEVICE_ID} isAdmin />);

    // Row "Delete" button opens the ConfirmDialog (sets deleteTarget).
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    // The ConfirmDialog (title "Delete Port") is rendered after the table, so
    // its confirm button is the last "Delete"-named button in the DOM.
    // jsdom keeps a closed <dialog>'s contents out of the accessibility tree,
    // so getByRole("button") cannot see the confirm button; query by text.
    const confirmDialog = screen
      .getByText("Delete Port")
      .closest("dialog") as HTMLDialogElement;
    const confirmButton = within(confirmDialog).getByText(
      "Delete",
    ) as HTMLButtonElement;
    fireEvent.click(confirmButton);

    await waitFor(() => expect(mockDeletePort).toHaveBeenCalledWith("port-1"));
    expect(mockToastSuccess).toHaveBeenCalledWith("Port deleted");
  });
});
