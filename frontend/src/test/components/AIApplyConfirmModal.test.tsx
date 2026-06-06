import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

import { server } from "../mocks/server";
import { AIApplyConfirmModal } from "@/components/reservations/AIApplyConfirmModal";

const PENDING = {
  job_id: "job-1",
  version_id: "ver-1",
  device_id: "dev-1",
  dry_run: true,
  scheduled_for: "2026-05-26T12:00:00Z",
};

function renderModal(planText = "I drafted vlan 200 and scheduled a dry-run.") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onClose = vi.fn();
  const utils = render(
    <QueryClientProvider client={client}>
      <AIApplyConfirmModal pendingApply={PENDING} planText={planText} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { ...utils, onClose };
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
});

describe("AIApplyConfirmModal", () => {
  it("renders the plan text", () => {
    renderModal("test plan body");
    expect(screen.getByText("test plan body")).toBeInTheDocument();
  });

  it("shows the transcript with simulated badges once the dry-run succeeds", async () => {
    server.use(
      http.get("/api/inventory/apply-jobs/job-1", () =>
        HttpResponse.json({
          id: "job-1",
          device_id: "dev-1",
          version_id: "ver-1",
          scheduled_for: PENDING.scheduled_for,
          reservation_id: null,
          dry_run: true,
          status: "success",
          run_id: "run-1",
          error: null,
          created_by: "u",
          author_name: "ai",
          created_at: "2026-05-26T12:00:00Z",
          fired_at: "2026-05-26T12:00:05Z",
        }),
      ),
      http.get("/api/execution/runs/run-1/commands", () =>
        HttpResponse.json([
          {
            id: "c1",
            run_id: "run-1",
            seq: 1,
            command: "vlan 200",
            response: "(simulated)",
            duration_ms: 3,
            exit_status: "simulated",
            created_at: "2026-05-26T12:00:05Z",
          },
        ]),
      ),
    );

    renderModal();
    await waitFor(() => expect(screen.getByText("Dry-run succeeded.")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("vlan 200")).toBeInTheDocument());
    expect(screen.getByText("simulated")).toBeInTheDocument();
  });

  it("disables Confirm until the dry-run succeeds", async () => {
    server.use(
      http.get("/api/inventory/apply-jobs/job-1", () =>
        HttpResponse.json({
          id: "job-1",
          device_id: "dev-1",
          version_id: "ver-1",
          scheduled_for: PENDING.scheduled_for,
          reservation_id: null,
          dry_run: true,
          status: "pending",
          run_id: null,
          error: null,
          created_by: "u",
          author_name: "ai",
          created_at: "2026-05-26T12:00:00Z",
          fired_at: null,
        }),
      ),
    );

    renderModal();
    await waitFor(() => expect(screen.getByText(/Waiting for dry-run/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Confirm and apply/i })).toBeDisabled();
  });

  it("Confirm POSTs to the confirm endpoint and closes", async () => {
    server.use(
      http.get("/api/inventory/apply-jobs/job-1", () =>
        HttpResponse.json({
          id: "job-1",
          device_id: "dev-1",
          version_id: "ver-1",
          scheduled_for: PENDING.scheduled_for,
          reservation_id: null,
          dry_run: true,
          status: "success",
          run_id: "run-1",
          error: null,
          created_by: "u",
          author_name: "ai",
          created_at: "2026-05-26T12:00:00Z",
          fired_at: "2026-05-26T12:00:05Z",
        }),
      ),
      http.get("/api/execution/runs/run-1/commands", () => HttpResponse.json([])),
      http.post("/api/inventory/apply-jobs/job-1/confirm", () =>
        HttpResponse.json(
          {
            id: "job-2",
            device_id: "dev-1",
            version_id: "ver-1",
            scheduled_for: PENDING.scheduled_for,
            reservation_id: null,
            dry_run: false,
            status: "pending",
            run_id: null,
            error: null,
            created_by: "confirmer",
            author_name: "confirmer",
            created_at: "2026-05-26T12:00:10Z",
            fired_at: null,
          },
          { status: 201 },
        ),
      ),
    );

    const { onClose } = renderModal();
    await waitFor(() => expect(screen.getByText("Dry-run succeeded.")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Confirm and apply/i }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it("Cancel button cancels the dry-run job and closes", async () => {
    server.use(
      http.get("/api/inventory/apply-jobs/job-1", () =>
        HttpResponse.json({
          id: "job-1",
          device_id: "dev-1",
          version_id: "ver-1",
          scheduled_for: PENDING.scheduled_for,
          reservation_id: null,
          dry_run: true,
          status: "pending",
          run_id: null,
          error: null,
          created_by: "u",
          author_name: "ai",
          created_at: "2026-05-26T12:00:00Z",
          fired_at: null,
        }),
      ),
      http.delete("/api/inventory/apply-jobs/job-1", () => new HttpResponse(null, { status: 204 })),
    );

    const { onClose } = renderModal();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Cancel dry-run/i })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Cancel dry-run/i }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it("Confirm endpoint 409 (failed dry-run) surfaces detail as toast", async () => {
    server.use(
      http.get("/api/inventory/apply-jobs/job-1", () =>
        HttpResponse.json({
          id: "job-1",
          device_id: "dev-1",
          version_id: "ver-1",
          scheduled_for: PENDING.scheduled_for,
          reservation_id: null,
          dry_run: true,
          status: "success",
          run_id: "run-1",
          error: null,
          created_by: "u",
          author_name: "ai",
          created_at: "2026-05-26T12:00:00Z",
          fired_at: "2026-05-26T12:00:05Z",
        }),
      ),
      http.get("/api/execution/runs/run-1/commands", () => HttpResponse.json([])),
      http.post("/api/inventory/apply-jobs/job-1/confirm", () =>
        HttpResponse.json({ detail: "Source job is not a dry-run" }, { status: 409 }),
      ),
    );

    renderModal();
    await waitFor(() => expect(screen.getByText("Dry-run succeeded.")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Confirm and apply/i }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls[0][0]).toMatch(/not a dry-run/i);
  });
});
