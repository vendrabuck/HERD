import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock react-router-dom; id is controlled per test via mockParamsId.
const mockNavigate = vi.fn();
let mockParamsId = "tmpl-1";
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
  useParams: () => ({ id: mockParamsId }),
}));

// Mock react-hot-toast
const mockToastError = vi.fn();
const mockToastSuccess = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: {
    error: (...args: unknown[]) => mockToastError(...args),
    success: (...args: unknown[]) => mockToastSuccess(...args),
  },
}));

// Mock authStore
let mockUser: { id: string; role: string } | null = { id: "1", role: "admin" };
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: typeof mockUser }) => unknown) =>
    selector({ user: mockUser }),
}));

// Mock template hooks
const mockUseTemplate = vi.fn();
const mockCreateTemplate = { mutateAsync: vi.fn(), isPending: false };
const mockUpdateTemplate = { mutateAsync: vi.fn(), isPending: false };
vi.mock("@/api/templates", () => ({
  useTemplate: (...args: unknown[]) => mockUseTemplate(...args),
  useCreateTemplate: () => mockCreateTemplate,
  useUpdateTemplate: () => mockUpdateTemplate,
}));

// Mock driver hooks
const mockUseDrivers = vi.fn();
vi.mock("@/api/drivers", () => ({
  useDrivers: () => mockUseDrivers(),
}));

// Mock hypervisor hooks
const mockUseHypervisors = vi.fn();
vi.mock("@/api/hypervisors", () => ({
  useHypervisors: () => mockUseHypervisors(),
}));

// Mock AI hooks
const mockUseAIStatus = vi.fn();
const mockSuggestIdentity = { mutateAsync: vi.fn(), isPending: false };
vi.mock("@/api/ai", () => ({
  useAIStatus: () => mockUseAIStatus(),
  useSuggestTemplateIdentity: () => mockSuggestIdentity,
}));

import { TemplateEditorPage } from "@/pages/TemplateEditorPage";

const DEVICE_DRIVER = {
  id: "d1",
  name: "Junos Driver",
  description: "fw",
  connection_type: "Management",
  filename: "junos.zip",
  size_bytes: 1024,
  sha256: "abc",
  uploaded_by: "admin",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const EXISTING_DEVICE_TEMPLATE = {
  id: "tmpl-1",
  name: "Edge Router",
  template_type: "device",
  driver_id: "d1",
  driver_name: "Junos Driver",
  connection_type: "Management",
  hypervisor_id: null,
  exclusive: true,
  icon: null,
  description: "existing description",
  vendor: "Cisco",
  model: "ISR4451",
  part_number: "ISR4451-X/K9",
  poll_interval_seconds: 60,
  sections: [
    {
      name: "General",
      fields: [{ key: "hostname", label: "Hostname", type: "string", required: true }],
    },
  ],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-05T00:00:00Z",
};

function selectSave() {
  return screen.getByRole("button", { name: "Save" });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockParamsId = "tmpl-1";
  mockUser = { id: "1", role: "admin" };
  mockUseTemplate.mockReturnValue({ data: EXISTING_DEVICE_TEMPLATE, isLoading: false });
  mockUseDrivers.mockReturnValue({ data: [DEVICE_DRIVER] });
  mockUseHypervisors.mockReturnValue({ data: [] });
  mockUseAIStatus.mockReturnValue({ data: { enabled: false } });
  mockCreateTemplate.mutateAsync.mockResolvedValue({});
  mockUpdateTemplate.mutateAsync.mockResolvedValue({});
});

describe("TemplateEditorPage loading and not-found states", () => {
  it("shows a loading message while an existing template is loading", () => {
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: true });
    render(<TemplateEditorPage />);
    expect(screen.getByText("Loading template...")).toBeInTheDocument();
  });

  it("shows a not-found message when the fetch finished with no data", () => {
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: false });
    render(<TemplateEditorPage />);
    expect(screen.getByText("Template not found")).toBeInTheDocument();
  });
});

describe("TemplateEditorPage view mode (existing template, not editing)", () => {
  it("renders the read-only summary fields", () => {
    render(<TemplateEditorPage />);
    expect(screen.getByText("Template Details")).toBeInTheDocument();
    expect(screen.getByText("Edge Router")).toBeInTheDocument();
    expect(screen.getByText("existing description")).toBeInTheDocument();
    expect(screen.getByText("Junos Driver (Management)")).toBeInTheDocument();
    expect(screen.getByText("Cisco")).toBeInTheDocument();
    expect(screen.getByText("ISR4451")).toBeInTheDocument();
    expect(screen.getByText("ISR4451-X/K9")).toBeInTheDocument();
    expect(screen.getByText("60s")).toBeInTheDocument();
  });

  it("renders section fields in the summary table with type labels and required flags", () => {
    render(<TemplateEditorPage />);
    expect(screen.getByText("General")).toBeInTheDocument();
    expect(screen.getByText("hostname")).toBeInTheDocument();
    expect(screen.getByText("Hostname")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
  });

  it("shows 'Not polled' when poll_interval_seconds is null", () => {
    mockUseTemplate.mockReturnValue({
      data: { ...EXISTING_DEVICE_TEMPLATE, poll_interval_seconds: null },
      isLoading: false,
    });
    render(<TemplateEditorPage />);
    expect(screen.getByText("Not polled")).toBeInTheDocument();
  });

  it("shows Edit button for an admin and none of the create-mode form fields", () => {
    render(<TemplateEditorPage />);
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
  });

  it("hides the Edit button for a non-admin", () => {
    mockUser = { id: "1", role: "user" };
    render(<TemplateEditorPage />);
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("clicking Back navigates to the templates list", () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(mockNavigate).toHaveBeenCalledWith("/templates");
  });
});

describe("TemplateEditorPage edit mode: entering, cancel, and save/update", () => {
  it("clicking Edit switches to the form and pre-fills fields from the existing template", () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Edge Router");
    expect(screen.getByLabelText("Vendor *")).toHaveValue("Cisco");
    expect(screen.getByLabelText("Model *")).toHaveValue("ISR4451");
  });

  it("Type select is disabled once editing an existing template", () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Type")).toBeDisabled();
  });

  it("Cancel restores fields to the existing template's values and exits edit mode", () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Mutated Name" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(screen.getByText("Edge Router")).toBeInTheDocument();
  });

  it("Save on an existing template calls update (not create) with id and no template_type in the payload", async () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edge Router v2" } });
    fireEvent.click(selectSave());

    await waitFor(() => expect(mockUpdateTemplate.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockCreateTemplate.mutateAsync).not.toHaveBeenCalled();
    const call = mockUpdateTemplate.mutateAsync.mock.calls[0][0];
    expect(call.id).toBe("tmpl-1");
    expect(call.data.name).toBe("Edge Router v2");
    expect(call.data).not.toHaveProperty("template_type");
    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalledWith("Template updated"));
  });

  it("a successful update exits edit mode without navigating away", async () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(selectSave());
    await waitFor(() => expect(mockUpdateTemplate.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
  });

  it("surfaces the backend detail toast on a failed save", async () => {
    mockUpdateTemplate.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "name already taken" } },
    });
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(selectSave());
    await waitFor(() => expect(mockToastError).toHaveBeenCalledWith("name already taken"));
  });

  it("falls back to a generic save-failure message with no response detail", async () => {
    mockUpdateTemplate.mutateAsync.mockRejectedValueOnce(new Error("network down"));
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(selectSave());
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Failed to save template"),
    );
  });
});

describe("TemplateEditorPage validation on save", () => {
  it("blocks save with 'Name is required' when the name is blank", () => {
    mockParamsId = "new";
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: false });
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Vendor *"), { target: { value: "Cisco" } });
    fireEvent.change(screen.getByLabelText("Model *"), { target: { value: "X" } });
    fireEvent.change(screen.getByLabelText("Driver"), { target: { value: "d1" } });
    fireEvent.click(selectSave());
    expect(mockToastError).toHaveBeenCalledWith("Name is required");
    expect(mockCreateTemplate.mutateAsync).not.toHaveBeenCalled();
  });

  it("blocks save with the vendor/model message when identity is missing", () => {
    mockParamsId = "new";
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: false });
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Tmpl" } });
    fireEvent.change(screen.getByLabelText("Driver"), { target: { value: "d1" } });
    // Save is disabled while !identityValid on create, so the button click is
    // a no-op; assert disabled instead of the toast for this path.
    expect(selectSave()).toBeDisabled();
  });

  it("blocks save with 'At least one section is required' once all sections are removed", () => {
    mockParamsId = "new";
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: false });
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Tmpl" } });
    fireEvent.change(screen.getByLabelText("Vendor *"), { target: { value: "Cisco" } });
    fireEvent.change(screen.getByLabelText("Model *"), { target: { value: "X" } });
    fireEvent.change(screen.getByLabelText("Driver"), { target: { value: "d1" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Remove Section" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "Remove Section" })[0]);
    fireEvent.click(selectSave());
    expect(mockToastError).toHaveBeenCalledWith("At least one section is required");
  });

  it("rejects a poll interval below the 30-second floor", () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Health-poll interval (seconds)"), {
      target: { value: "10" },
    });
    fireEvent.click(selectSave());
    expect(mockToastError).toHaveBeenCalledWith(
      "Poll interval must be a whole number >= 30 (or blank for none)",
    );
    expect(mockUpdateTemplate.mutateAsync).not.toHaveBeenCalled();
  });

  it("rejects a non-integer poll interval", () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Health-poll interval (seconds)"), {
      target: { value: "45.5" },
    });
    fireEvent.click(selectSave());
    expect(mockToastError).toHaveBeenCalledWith(
      "Poll interval must be a whole number >= 30 (or blank for none)",
    );
  });

  it("accepts a blank poll interval as null (no polling)", async () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Health-poll interval (seconds)"), {
      target: { value: "" },
    });
    fireEvent.click(selectSave());
    await waitFor(() => expect(mockUpdateTemplate.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockUpdateTemplate.mutateAsync.mock.calls[0][0].data.poll_interval_seconds).toBeNull();
  });
});

describe("TemplateEditorPage section and field CRUD", () => {
  beforeEach(() => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  });

  it("adds a new empty section via + Add Section", () => {
    fireEvent.click(screen.getByRole("button", { name: "+ Add Section" }));
    const sectionNameInputs = screen.getAllByPlaceholderText("Section name");
    expect(sectionNameInputs).toHaveLength(2);
  });

  it("renaming a section updates its name input value", () => {
    const sectionInput = screen.getByPlaceholderText("Section name");
    fireEvent.change(sectionInput, { target: { value: "Renamed Section" } });
    expect(sectionInput).toHaveValue("Renamed Section");
  });

  it("removing a section removes its field rows too", () => {
    expect(screen.getByPlaceholderText("Key")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove Section" }));
    expect(screen.queryByPlaceholderText("Key")).not.toBeInTheDocument();
  });

  it("+ Add Field appends a blank field row to the section", () => {
    expect(screen.getAllByPlaceholderText("Key")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "+ Add Field" }));
    expect(screen.getAllByPlaceholderText("Key")).toHaveLength(2);
    // The new row starts blank.
    expect(screen.getAllByPlaceholderText("Key")[1]).toHaveValue("");
  });

  it("editing a field's key updates that row without affecting other fields", () => {
    fireEvent.click(screen.getByRole("button", { name: "+ Add Field" }));
    const keyInputs = screen.getAllByPlaceholderText("Key");
    fireEvent.change(keyInputs[1], { target: { value: "serial_number" } });
    expect(keyInputs[0]).toHaveValue("hostname");
    expect(screen.getAllByPlaceholderText("Key")[1]).toHaveValue("serial_number");
  });

  it("removing a field via its row Remove button drops only that row", () => {
    fireEvent.click(screen.getByRole("button", { name: "+ Add Field" }));
    fireEvent.change(screen.getAllByPlaceholderText("Key")[1], {
      target: { value: "serial_number" },
    });
    fireEvent.click(screen.getAllByTitle("Remove field")[0]);
    const remaining = screen.getAllByPlaceholderText("Key");
    expect(remaining).toHaveLength(1);
    expect(remaining[0]).toHaveValue("serial_number");
  });

  it("saving carries edited section/field state through to the update payload", async () => {
    fireEvent.click(screen.getByRole("button", { name: "+ Add Field" }));
    fireEvent.change(screen.getAllByPlaceholderText("Key")[1], { target: { value: "serial" } });
    fireEvent.change(screen.getAllByPlaceholderText("Label")[1], { target: { value: "Serial" } });
    fireEvent.click(selectSave());

    await waitFor(() => expect(mockUpdateTemplate.mutateAsync).toHaveBeenCalledTimes(1));
    const sections = mockUpdateTemplate.mutateAsync.mock.calls[0][0].data.sections;
    expect(sections[0].fields).toHaveLength(2);
    expect(sections[0].fields[1]).toMatchObject({ key: "serial", label: "Serial" });
  });
});

describe("TemplateEditorPage dynamic-vs-physical field toggling", () => {
  beforeEach(() => {
    mockParamsId = "new";
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: false });
    mockUseHypervisors.mockReturnValue({
      data: [
        {
          id: "h1",
          name: "Proxmox Lab",
          description: null,
          endpoint: "https://proxmox.local:8006",
          hypervisor_type: "proxmox",
          secret_id: "s1",
          enabled: true,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          modified_by: null,
        },
      ],
    });
    mockUseDrivers.mockReturnValue({
      data: [
        DEVICE_DRIVER,
        {
          id: "d2",
          name: "Proxmox Recipe",
          description: "recipe",
          connection_type: "Hypervisor",
          filename: "proxmox.tar.gz",
          size_bytes: 1,
          sha256: "x",
          uploaded_by: "admin",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
  });

  it("Hardware Identity block is present for device type and absent for dynamic", () => {
    render(<TemplateEditorPage />);
    expect(screen.getByText("Hardware Identity")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    expect(screen.queryByText("Hardware Identity")).not.toBeInTheDocument();
  });

  it("shows the amber hint when no Hypervisor-type drivers are uploaded", () => {
    mockUseDrivers.mockReturnValue({ data: [DEVICE_DRIVER] });
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    expect(
      screen.getByText(/No Hypervisor-type drivers uploaded yet/),
    ).toBeInTheDocument();
  });

  it("shows the amber hint when no hypervisors are registered", () => {
    mockUseHypervisors.mockReturnValue({ data: [] });
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    expect(screen.getByText(/No hypervisors registered/)).toBeInTheDocument();
  });

  it("a fully valid dynamic template creates with driver_id and hypervisor_id, no vendor/model required", async () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Ubuntu VM" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    fireEvent.change(screen.getByLabelText("Recipe Driver"), { target: { value: "d2" } });
    fireEvent.change(screen.getByLabelText("Hypervisor"), { target: { value: "h1" } });
    fireEvent.click(selectSave());

    await waitFor(() => expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "Ubuntu VM",
        template_type: "dynamic",
        driver_id: "d2",
        hypervisor_id: "h1",
        vendor: undefined,
        model: undefined,
      }),
    );
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/templates"));
  });
});

describe("TemplateEditorPage AI suggest-identity", () => {
  beforeEach(() => {
    mockUseAIStatus.mockReturnValue({ data: { enabled: true } });
  });

  it("shows the Suggest with AI button only when AI is enabled", () => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("button", { name: "Suggest with AI" })).toBeInTheDocument();
  });

  it("hides Suggest with AI when the status reports disabled", () => {
    mockUseAIStatus.mockReturnValue({ data: { enabled: false } });
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.queryByRole("button", { name: "Suggest with AI" })).not.toBeInTheDocument();
  });

  it("blocks suggest with a toast when the name is blank", async () => {
    mockParamsId = "new";
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: false });
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Suggest with AI" }));
    expect(mockToastError).toHaveBeenCalledWith(
      "Enter a template name first; the AI uses it as the main signal.",
    );
    expect(mockSuggestIdentity.mutateAsync).not.toHaveBeenCalled();
  });

  it("applies the suggested vendor/model/part_number and toasts a success summary", async () => {
    mockSuggestIdentity.mutateAsync.mockResolvedValue({
      vendor: "Arista",
      model: "7050X3",
      part_number: "DCS-7050SX3-48YC8",
      confidence: "high",
    });
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Suggest with AI" }));

    await waitFor(() => expect(screen.getByLabelText("Vendor *")).toHaveValue("Arista"));
    expect(screen.getByLabelText("Model *")).toHaveValue("7050X3");
    expect(screen.getByLabelText("Part Number")).toHaveValue("DCS-7050SX3-48YC8");
    expect(mockToastSuccess).toHaveBeenCalledWith(
      "Suggested: Arista, 7050X3 (confidence: high)",
    );
  });

  it("does not overwrite part number when the suggestion omits it", async () => {
    mockSuggestIdentity.mutateAsync.mockResolvedValue({
      vendor: "Arista",
      model: "7050X3",
      part_number: null,
      confidence: "medium",
    });
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Part Number"), { target: { value: "kept-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Suggest with AI" }));

    await waitFor(() => expect(screen.getByLabelText("Vendor *")).toHaveValue("Arista"));
    expect(screen.getByLabelText("Part Number")).toHaveValue("kept-value");
  });

  it("surfaces the backend detail toast on a suggest failure", async () => {
    mockSuggestIdentity.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "AI provider unavailable" } },
    });
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Suggest with AI" }));
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("AI provider unavailable"),
    );
  });
});

describe("TemplateEditorPage icon upload", () => {
  beforeEach(() => {
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  });

  it("rejects a file over 256 KB with a toast and does not set the icon", () => {
    const bigFile = new File([new Uint8Array(256 * 1024 + 1)], "big.png", {
      type: "image/png",
    });
    fireEvent.change(screen.getByLabelText("Icon"), { target: { files: [bigFile] } });
    expect(mockToastError).toHaveBeenCalledWith("Icon must be under 256 KB");
    expect(screen.queryByAltText("Template icon")).not.toBeInTheDocument();
  });

});

describe("TemplateEditorPage icon remove", () => {
  it("Remove clears a previously set icon and hides the preview", () => {
    mockUseTemplate.mockReturnValue({
      data: { ...EXISTING_DEVICE_TEMPLATE, icon: "data:image/png;base64,abc123" },
      isLoading: false,
    });
    render(<TemplateEditorPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByAltText("Template icon")).toBeInTheDocument();
    // The icon Remove button sits beside the file input; other same-labelled
    // Remove buttons belong to field rows (title="Remove field") and are
    // matched away here by their distinct accessible name role query below.
    const iconRemove = screen
      .getAllByRole("button", { name: "Remove" })
      .find((btn) => btn.getAttribute("title") !== "Remove field")!;
    fireEvent.click(iconRemove);
    expect(screen.queryByAltText("Template icon")).not.toBeInTheDocument();
  });
});
