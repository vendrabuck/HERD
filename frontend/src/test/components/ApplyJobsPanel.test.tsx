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
import { PageCapNotice } from "@/components/ui/PageCapNotice";
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
    // A list entry without a string msg has no readable text (issue #1314).
    server.use(
      http.get(JOBS_URL, () => HttpResponse.json(page([makeJob()]))),
      http.delete("/api/inventory/apply-jobs/:id", () =>
        HttpResponse.json({ detail: [{ type: "uuid_parsing" }] }, { status: 422 }),
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

const NGINX_503 = "This part of HERD is not answering right now. Try again in a moment.";
const SPA_PAGE =
  '<!doctype html><html><head><title>HERD</title></head><body><div id="root"></div></body></html>';
const CAP_LINE = /^Showing \d+ of \d+ scheduled applies, latest scheduled first\.$/;

type Answer = "server" | "nginx" | "html" | "empty" | "jobs";

/** One answer of the jobs route: a failure of one of three kinds, no jobs, or one pending job. */
function answer(kind: Answer) {
  switch (kind) {
    case "server":
      return HttpResponse.json({ detail: "inventory is restarting" }, { status: 500 });
    case "nginx":
      return HttpResponse.json({ detail: NGINX_503 }, { status: 503 });
    case "html":
      return new HttpResponse(SPA_PAGE, { status: 200, headers: { "Content-Type": "text/html" } });
    case "empty":
      return HttpResponse.json(page([]));
    default:
      return HttpResponse.json(page([makeJob({ author_name: "alice" })]));
  }
}

describe("ApplyJobsPanel load states (CFG-UI-5, issue #1590)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    ["server" as const, "inventory is restarting"],
    ["nginx" as const, NGINX_503],
    ["html" as const, "The server's answer could not be read."],
  ])("a %s failure shows the load error under the heading, and Retry loads the rows", async (kind, reason) => {
    let next: Answer = kind;
    server.use(http.get(JOBS_URL, () => answer(next)));
    const user = userEvent.setup();
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not load scheduled applies. ${reason}`);
    expect(screen.getByText("Scheduled applies")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    next = "jobs";
    await user.click(screen.getByRole("button", { name: "Retry loading scheduled applies" }));
    expect(await screen.findByText("alice")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("a failed poll keeps the rows under a refresh notice, and the next good poll clears it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let next: Answer = "jobs";
    server.use(http.get(JOBS_URL, () => answer(next)));
    renderPanel();
    await screen.findByText("alice");

    next = "server";
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Could not refresh scheduled applies. inventory is restarting",
    );
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    next = "jobs";
    await vi.advanceTimersByTimeAsync(10_000);
    await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
    expect(screen.getByText("alice")).toBeInTheDocument();
  });

  it("no jobs, then a failed poll, shows the heading and the refresh notice; a good empty poll hides the panel", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let next: Answer = "empty";
    let gets = 0;
    server.use(
      http.get(JOBS_URL, () => {
        gets += 1;
        return answer(next);
      }),
    );
    const { container } = renderPanel();
    await waitFor(() => expect(gets).toBe(1));
    await waitFor(() => expect(container).toBeEmptyDOMElement());

    next = "server";
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Could not refresh scheduled applies. inventory is restarting",
    );
    expect(screen.getByText("Scheduled applies")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    next = "empty";
    await vi.advanceTimersByTimeAsync(10_000);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe("ApplyJobsPanel reads one page and says more exist (CFG-UI-5, issue #1590)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const FIFTY = Array.from({ length: 50 }, (_, i) =>
    makeJob({ id: `job-${i}`, author_name: `author-${i}` }),
  );

  it("50 jobs of 73 show the cap line, one page per poll and no page walk", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const asked: URLSearchParams[] = [];
    server.use(
      http.get(JOBS_URL, ({ request }) => {
        asked.push(new URL(request.url).searchParams);
        return HttpResponse.json({ items: FIFTY, total: 73, skip: 0, limit: 50 });
      }),
    );
    renderPanel();

    expect(
      await screen.findByText("Showing 50 of 73 scheduled applies, latest scheduled first."),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Cancel" })).toHaveLength(50);
    expect(asked).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(10_000);
    await waitFor(() => expect(asked).toHaveLength(2));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(asked).toHaveLength(2);
    for (const params of asked) {
      expect(params.get("skip")).toBe("0");
      expect(params.get("limit")).toBe("50");
    }
  });

  it.each([
    ["a total of 50", 50],
    ["no total", undefined],
  ])("%s shows no cap line", async (_label, total) => {
    server.use(
      http.get(JOBS_URL, () => HttpResponse.json({ items: FIFTY, total, skip: 0, limit: 50 })),
    );
    renderPanel();
    expect(await screen.findByText("author-49")).toBeInTheDocument();
    expect(screen.queryByText(CAP_LINE)).not.toBeInTheDocument();
  });
});

describe("PageCapNotice (issue #1590)", () => {
  it("names the page, the total, and the order", () => {
    render(<PageCapNotice shown={50} total={73} noun="scheduled applies" order="latest scheduled first" />);
    expect(
      screen.getByText("Showing 50 of 73 scheduled applies, latest scheduled first."),
    ).toBeInTheDocument();
  });

  it.each([
    ["every row shown", 50, 50],
    ["a total that lags", 50, 49],
    ["no total", 50, undefined],
    ["a total that is not a number", 50, "73"],
    ["an infinite total", 50, Infinity],
  ])("renders nothing for %s", (_label, shown, total) => {
    const { container } = render(
      <PageCapNotice shown={shown} total={total} noun="scheduled applies" order="latest scheduled first" />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
