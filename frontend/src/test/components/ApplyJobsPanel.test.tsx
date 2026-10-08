import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { http, HttpResponse } from "msw";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { server } from "../mocks/server";

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: {
    success: (m: unknown) => toastSuccess(m),
    error: (m: unknown) => toastError(m),
  },
}));

import { ApplyJobsPanel } from "@/components/device-config/ApplyJobsPanel";
import type { ApplyJob } from "@/api/deviceConfigJobs";

const DEVICE_ID = "device-1";
const JOBS_URL = `/api/inventory/devices/${DEVICE_ID}/apply-jobs`;

function makeJob(overrides: Partial<ApplyJob> = {}): ApplyJob {
  return {
    id: "job-1",
    device_id: DEVICE_ID,
    version_id: "ver-1",
    scheduled_for: "2030-01-02T03:04:00Z",
    reservation_id: null,
    dry_run: false,
    status: "pending",
    run_id: null,
    error: null,
    created_by: "abcdef0123456789",
    author_name: "alice",
    created_at: "2026-10-01T00:00:00Z",
    fired_at: null,
    ...overrides,
  };
}

function page(items: ApplyJob[]) {
  return { items, total: items.length, skip: 0, limit: 50 };
}

function renderPanel(ui: ReactNode = <ApplyJobsPanel deviceId={DEVICE_ID} />) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function rowFor(text: string): HTMLElement {
  return screen.getByText(text).closest("tr") as HTMLElement;
}

describe("ApplyJobsPanel (CFG-UI-5)", () => {
  beforeEach(() => {
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders nothing when the device has no scheduled applies", async () => {
    let fetched = false;
    server.use(
      http.get(JOBS_URL, () => {
        fetched = true;
        return HttpResponse.json(page([]));
      }),
    );
    const { container } = renderPanel();
    await waitFor(() => expect(fetched).toBe(true));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(screen.queryByText("Scheduled applies")).not.toBeInTheDocument();
  });

  it("shows each job's status, error, author, and run, with Cancel only on pending", async () => {
    server.use(
      http.get(JOBS_URL, () =>
        HttpResponse.json(
          page([
            makeJob({ id: "job-p", status: "pending", author_name: "alice" }),
            makeJob({
              id: "job-f",
              status: "failed",
              author_name: "bob",
              run_id: "run-87654321-ffff",
              error: "execution answered HTTP 409",
            }),
            makeJob({ id: "job-s", status: "success", author_name: "", created_by: "zz99yy88xx" }),
          ]),
        ),
      ),
    );
    renderPanel();

    // The heading shows while the first read is in flight; wait for the rows.
    await screen.findByText("alice");
    expect(screen.getByText("Scheduled applies")).toBeInTheDocument();
    const pending = rowFor("alice");
    expect(within(pending).getByText("pending")).toBeInTheDocument();
    expect(within(pending).getByRole("button", { name: "Cancel" })).toBeInTheDocument();

    const failed = rowFor("bob");
    expect(within(failed).getByText("(execution answered HTTP 409)")).toBeInTheDocument();
    expect(within(failed).getByText("run-8765")).toBeInTheDocument();
    expect(within(failed).queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();

    // No author name falls back to the first 8 characters of the creator id.
    const done = rowFor("zz99yy88");
    expect(within(done).getByText("success")).toBeInTheDocument();
    expect(within(done).queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Cancel" })).toHaveLength(1);
  });

  it("cancels a pending job through the API and confirms with a toast", async () => {
    let deleted: string | null = null;
    server.use(
      http.get(JOBS_URL, () => HttpResponse.json(page([makeJob({ id: "job-p" })]))),
      http.delete("/api/inventory/apply-jobs/:id", ({ params }) => {
        deleted = params.id as string;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Cancelled"));
    expect(deleted).toBe("job-p");
  });

  it("shows the server's sentence when a cancel loses the race (409)", async () => {
    server.use(
      http.get(JOBS_URL, () => HttpResponse.json(page([makeJob()]))),
      http.delete("/api/inventory/apply-jobs/:id", () =>
        HttpResponse.json({ detail: "Job is 'running', not cancellable" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Job is 'running', not cancellable"),
    );
  });

  it("never toasts an object detail; a non-string refusal falls back", async () => {
    server.use(
      http.get(JOBS_URL, () => HttpResponse.json(page([makeJob()]))),
      http.delete("/api/inventory/apply-jobs/:id", () =>
        HttpResponse.json({ detail: [{ msg: "bad id" }] }, { status: 422 }),
      ),
    );
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Cancel failed"));
  });

  it("refreshes the job list every 10 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let gets = 0;
    server.use(
      http.get(JOBS_URL, () => {
        gets += 1;
        return HttpResponse.json(page([makeJob()]));
      }),
    );
    renderPanel();
    await screen.findByText("alice");
    expect(gets).toBe(1);
    await vi.advanceTimersByTimeAsync(10_000);
    await waitFor(() => expect(gets).toBe(2));
  });
});
