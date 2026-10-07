import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  // jsdom does not implement these; emulate the open-state toggle so
  // ARIA queries can see content inside the dialog.
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
});

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

import { server } from "../mocks/server";
import { AIDialog } from "@/components/topology-editor/AIDialog";

function renderDialog(onProposal = vi.fn(), onClose = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrap = (children: ReactNode) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return {
    onProposal,
    onClose,
    ...render(
      wrap(<AIDialog open={true} onClose={onClose} onProposal={onProposal} />),
    ),
  };
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
});

describe("AIDialog", () => {
  it("renders the dialog title and prompt textarea", () => {
    renderDialog();
    expect(screen.getByText("Generate topology with AI")).toBeInTheDocument();
    expect(screen.getByLabelText("Prompt")).toBeInTheDocument();
  });

  it("disables Generate when the prompt is empty", () => {
    renderDialog();
    const btn = screen.getByRole("button", { name: "Generate" });
    expect(btn).toBeDisabled();
  });

  it("toasts when the prompt is whitespace-only after clicking Generate", () => {
    renderDialog();
    // Force the button enabled via a non-empty value then strip with whitespace.
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "  " },
    });
    // Still disabled because trim is empty.
    expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
  });

  it("calls onProposal with the response on success", async () => {
    const proposal = {
      purpose: "two firewalls",
      devices: [],
      edges: [],
      notes: null,
      model: "claude-sonnet-4-6",
      input_tokens: 10,
      output_tokens: 5,
    };
    server.use(
      http.post("/api/ai/generate", () => HttpResponse.json(proposal)),
    );
    const { onProposal, onClose } = renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "two firewalls please" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(onProposal).toHaveBeenCalledTimes(1));
    expect(onProposal).toHaveBeenCalledWith(proposal);
    expect(onClose).toHaveBeenCalled();
  });

  it("surfaces the server detail on a 503 and never names ANTHROPIC_API_KEY", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        HttpResponse.json(
          { detail: "AI orchestrator is not configured" },
          { status: 503 },
        ),
      ),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const message = toastError.mock.calls[0][0] as string;
    // The server detail is provider-agnostic and must be surfaced verbatim.
    expect(message).toBe("AI orchestrator is not configured");
    // The old hardcoded message is wrong for keyless-anthropic and openai_compat.
    expect(message).not.toMatch(/ANTHROPIC_API_KEY/);
  });

  it("falls back to a provider-agnostic 503 message when the body has no detail", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        new HttpResponse(null, { status: 503 }),
      ),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const message = toastError.mock.calls[0][0] as string;
    expect(message).toMatch(/not configured/i);
    expect(message).toMatch(/administrator/i);
    expect(message).not.toMatch(/ANTHROPIC_API_KEY/);
  });

  it("names each unwireable connection on the structured 422", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        HttpResponse.json(
          {
            detail: {
              error: "topology_unconnectable",
              pairs: [
                {
                  source_role: "fw-a",
                  target_role: "client",
                  source_template: "EX3400",
                  target_template: "Ubuntu Client",
                },
              ],
              message: "The lab has no cabled path for 1 proposed connection.",
            },
          },
          { status: 422 },
        ),
      ),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const message = toastError.mock.calls[0][0] as string;
    expect(message).toContain("The lab has no cabled path for 1 proposed connection.");
    expect(message).toContain("fw-a to client");
    // Not the generic fallback: the proposal failed for a specific reason.
    expect(message).not.toBe("Failed to generate topology");
  });

  it("checks picked files client-side before sending (#1040)", () => {
    renderDialog();
    const input = screen.getByLabelText("Reference files (optional)");
    const big = new File(["x"], "big.txt", { type: "text/plain" });
    Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
    const okA = new File(["aaa"], "a.txt", { type: "text/plain" });
    const okADuplicate = new File(["aaa"], "a.txt", { type: "text/plain" });
    fireEvent.change(input, {
      target: {
        files: [new File(["x"], "tool.exe"), big, okA, okADuplicate],
      },
    });
    // An unaccepted extension and an oversize file are dropped with a toast;
    // the second a.txt (same name and size) is skipped silently.
    expect(toastError).toHaveBeenCalledWith("Unsupported file type: tool.exe");
    expect(toastError).toHaveBeenCalledWith("big.txt exceeds 5 MB limit");
    expect(toastError).toHaveBeenCalledTimes(2);
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(1);

    toastError.mockClear();
    fireEvent.change(input, {
      target: {
        files: ["b", "c", "d", "e", "f"].map((n) => new File([n], `${n}.md`)),
      },
    });
    // Picking stops at five files in all, with one toast.
    expect(toastError).toHaveBeenCalledWith("Limit is 5 files per request");
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(5);
  });

  it("prefixes a 400 with Upload rejected (#1040)", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        HttpResponse.json({ detail: "Too many files: limit is 5, got 6" }, { status: 400 }),
      ),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "go" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls[0][0]).toBe("Upload rejected: Too many files: limit is 5, got 6");
  });

  it("names the templates per type on the mixed-types 422 (#1038)", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        HttpResponse.json(
          {
            detail: {
              error: "topology_mixed_types",
              groups: [
                { topology_type: "CLOUD", roles: ["vm"], templates: ["CloudVM"] },
                { topology_type: "PHYSICAL", roles: ["fw"], templates: ["EX3400"] },
              ],
              message: "The proposal mixes CLOUD and PHYSICAL devices.",
            },
          },
          { status: 422 },
        ),
      ),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const message = toastError.mock.calls[0][0] as string;
    expect(message).toBe(
      "The proposal mixes CLOUD and PHYSICAL devices.\nCLOUD: CloudVM\nPHYSICAL: EX3400",
    );
  });

  it("keeps the plain-string fallback for a 422 that is not structured", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        HttpResponse.json({ detail: "Unprocessable" }, { status: 422 }),
      ),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls[0][0]).toBe("Failed to generate topology");
  });

  it("toasts the 502 upstream detail", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        HttpResponse.json(
          { detail: "model timeout" },
          { status: 502 },
        ),
      ),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls[0][0]).toMatch(/model timeout/);
  });
});
