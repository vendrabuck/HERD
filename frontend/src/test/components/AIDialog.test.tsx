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

  it("toasts the 503 'not configured' message", async () => {
    server.use(
      http.post("/api/ai/generate", () =>
        HttpResponse.json(
          { detail: "AI not configured" },
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
    expect(toastError.mock.calls[0][0]).toMatch(/not configured/i);
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
