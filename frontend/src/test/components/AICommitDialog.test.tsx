import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
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
import { AICommitDialog } from "@/components/topology-editor/AICommitDialog";
import type { AIGenerateResponse } from "@/types/ai.types";

const PROPOSAL: AIGenerateResponse = {
  purpose: "ha pair",
  notes: "",
  file_summaries: [],
  devices: [
    {
      role: "fw1",
      template_name: "PA",
      topology_type: "PHYSICAL",
      device: {
        id: "d1",
        name: "fw1",
        template_id: "t1",
        template_name: "PA",
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
        connection_type: "Management",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        created_by: null,
        created_by_name: null,
        modified_by: null,
        modified_by_name: null,
        poll_interval_seconds: null,
        resolved_poll_interval_seconds: null,
      },
      config: null,
    },
  ],
  edges: [],
};

function renderDialog(
  proposal: AIGenerateResponse | null = PROPOSAL,
  onCommitted = vi.fn(),
  onClose = vi.fn(),
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrap = (children: ReactNode) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return {
    onCommitted,
    onClose,
    ...render(
      wrap(
        <AICommitDialog
          open={true}
          proposal={proposal}
          onClose={onClose}
          onCommitted={onCommitted}
        />,
      ),
    ),
  };
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
});

describe("AICommitDialog", () => {
  it("renders the commit form with a default topology name and device summary", () => {
    renderDialog();
    expect(screen.getByText("Commit AI proposal")).toBeInTheDocument();
    expect(
      screen.getByText(/1 devices, 0 edges\./i),
    ).toBeInTheDocument();
    const name = screen.getByLabelText("Topology name") as HTMLInputElement;
    expect(name.value).toMatch(/^AI: ha pair/);
  });

  it("toasts an error when topology name is empty", async () => {
    renderDialog();
    const name = screen.getByLabelText("Topology name") as HTMLInputElement;
    fireEvent.change(name, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Topology name is required"),
    );
  });

  it("toasts an error when end time is not after start time", async () => {
    renderDialog();
    const start = screen.getByLabelText("Start time") as HTMLInputElement;
    const end = screen.getByLabelText("End time") as HTMLInputElement;
    fireEvent.change(end, { target: { value: start.value } });
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "End time must be after start time",
      ),
    );
  });

  it("calls onCommitted with the successful response", async () => {
    const response = {
      topology_id: "t-new",
      reservation_id: "r-new",
      config_results: [],
    };
    server.use(
      http.post("/api/ai/commit", () => HttpResponse.json(response)),
    );
    const { onCommitted } = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() => expect(onCommitted).toHaveBeenCalledWith(response));
    expect(toastSuccess).toHaveBeenCalledWith(
      "Topology and reservation created",
    );
  });

  it("toasts the backend detail when commit fails", async () => {
    server.use(
      http.post("/api/ai/commit", () =>
        HttpResponse.json({ detail: "device unavailable" }, { status: 409 }),
      ),
    );
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Commit failed: device unavailable",
      ),
    );
  });

  it("renders nothing inside the modal when proposal is null", () => {
    renderDialog(null);
    expect(screen.queryByLabelText("Topology name")).not.toBeInTheDocument();
  });
});
