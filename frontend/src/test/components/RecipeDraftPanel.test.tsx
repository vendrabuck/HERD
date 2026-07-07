import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import type { RecipeDraftResponse } from "@/types/ai.types";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

const mockToastError = vi.fn();
const mockToastSuccess = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: {
    error: (...args: unknown[]) => mockToastError(...args),
    success: (...args: unknown[]) => mockToastSuccess(...args),
  },
}));

const mockDraftMutate = vi.fn();
const mockRefineMutate = vi.fn();
vi.mock("@/api/recipes", async () => {
  const actual = await vi.importActual<typeof import("@/api/recipes")>("@/api/recipes");
  return {
    ...actual,
    useDraftRecipe: () => ({ mutateAsync: mockDraftMutate, isPending: false }),
    useRefineRecipeDraft: () => ({ mutateAsync: mockRefineMutate, isPending: false }),
  };
});

const mockCreateDriver = { mutateAsync: vi.fn(), isPending: false };
vi.mock("@/api/drivers", () => ({
  useCreateDriver: () => mockCreateDriver,
}));

import { RecipeDraftPanel } from "@/components/admin/RecipeDraftPanel";

// btoa("fake zip bytes") so package_b64 decodes without error.
const PACKAGE_B64 = btoa("fake zip bytes");

function draftResponse(overrides: Partial<RecipeDraftResponse> = {}): RecipeDraftResponse {
  return {
    draft_id: "11111111-2222-3333-4444-555555555555",
    valid: true,
    attempts: 1,
    model: "test-model",
    prompt: "clone a proxmox vm",
    hypervisor_type: "proxmox",
    explanation: "Clones a template VM via the Proxmox API.",
    driver_py: "class Driver:\n    pass\n",
    driver_metadata: {
      name: "proxmox-clone",
      version: "0.1.0",
      connection_type: "Hypervisor",
      supports_dry_run: true,
      draft_id: "11111111-2222-3333-4444-555555555555",
    },
    validation: {
      valid: true,
      structural: { passed: true, errors: [] },
      policy: { passed: true, errors: [] },
      schema: { present: false, schema: null, error: null },
      dry_run: {
        passed: true,
        methods: [
          {
            action: "login",
            passed: true,
            success: true,
            output: { success: true },
            error: null,
            duration_ms: 5,
            transcript: [],
          },
        ],
        error: null,
      },
    },
    package_b64: PACKAGE_B64,
    created_at: "2026-07-07T00:00:00Z",
    updated_at: "2026-07-07T00:00:00Z",
    ...overrides,
  };
}

async function draftInPanel(response: RecipeDraftResponse) {
  mockDraftMutate.mockResolvedValue(response);
  fireEvent.change(screen.getByLabelText("What should the recipe do?"), {
    target: { value: "clone a proxmox vm" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Draft", hidden: true }));
  await waitFor(() => expect(mockDraftMutate).toHaveBeenCalled());
}

describe("RecipeDraftPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("requires a prompt before drafting", () => {
    render(<RecipeDraftPanel open onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Draft", hidden: true }));
    expect(mockToastError).toHaveBeenCalledWith("Describe the recipe first");
    expect(mockDraftMutate).not.toHaveBeenCalled();
  });

  it("renders the draft, validation verdict, and code after drafting", async () => {
    render(<RecipeDraftPanel open onClose={vi.fn()} />);
    await draftInPanel(draftResponse());

    expect(await screen.findByText("Validation passed (1 attempt)")).toBeInTheDocument();
    expect(screen.getByText(/class Driver:/)).toBeInTheDocument();
    expect(screen.getByText("Clones a template VM via the Proxmox API.")).toBeInTheDocument();
    expect(screen.getByText(/1\/1 lifecycle methods passed/)).toBeInTheDocument();
    // The driver-name field prefills from the metadata.
    expect(screen.getByLabelText("Driver name")).toHaveValue("proxmox-clone");
  });

  it("approve uploads the package as a Hypervisor driver", async () => {
    mockCreateDriver.mutateAsync.mockResolvedValue({});
    render(<RecipeDraftPanel open onClose={vi.fn()} />);
    await draftInPanel(draftResponse());

    fireEvent.click(await screen.findByRole("button", { name: "Approve and upload", hidden: true }));
    await waitFor(() => expect(mockCreateDriver.mutateAsync).toHaveBeenCalled());

    const call = mockCreateDriver.mutateAsync.mock.calls[0][0];
    expect(call.connection_type).toBe("Hypervisor");
    expect(call.name).toBe("proxmox-clone");
    expect(call.file).toBeInstanceOf(File);
    expect(call.file.name).toBe("proxmox-clone.zip");
    expect(mockToastSuccess).toHaveBeenCalledWith("Recipe uploaded as a driver");
  });

  it("blocks approve for a draft that failed validation", async () => {
    render(<RecipeDraftPanel open onClose={vi.fn()} />);
    await draftInPanel(
      draftResponse({
        valid: false,
        attempts: 3,
        validation: {
          valid: false,
          structural: {
            passed: false,
            errors: ["Driver class is missing required method: status"],
          },
          policy: { passed: true, errors: [] },
          schema: { present: false, schema: null, error: null },
          dry_run: { passed: false, methods: [], error: "not run" },
        },
      }),
    );

    expect(await screen.findByText("Validation failed after 3 attempts")).toBeInTheDocument();
    expect(
      screen.getByText(/structural: Driver class is missing required method: status/),
    ).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "Approve and upload", hidden: true });
    expect(approve).toBeDisabled();
    expect(screen.getByText(/cannot be uploaded from here/)).toBeInTheDocument();
  });

  it("refine sends the draft id and feedback", async () => {
    render(<RecipeDraftPanel open onClose={vi.fn()} />);
    await draftInPanel(draftResponse());
    mockRefineMutate.mockResolvedValue(draftResponse({ attempts: 2 }));

    fireEvent.change(await screen.findByLabelText("Request changes"), {
      target: { value: "verify TLS" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Refine", hidden: true }));

    await waitFor(() =>
      expect(mockRefineMutate).toHaveBeenCalledWith({
        draft_id: "11111111-2222-3333-4444-555555555555",
        feedback: "verify TLS",
      }),
    );
  });

  it("surfaces the backend detail when drafting fails", async () => {
    mockDraftMutate.mockRejectedValue({
      response: { data: { detail: "AI recipe authoring is disabled" } },
    });
    render(<RecipeDraftPanel open onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("What should the recipe do?"), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Draft", hidden: true }));
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("AI recipe authoring is disabled"),
    );
  });
});
