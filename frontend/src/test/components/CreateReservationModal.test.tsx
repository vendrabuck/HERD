import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  // jsdom has no real dialog behavior. showModal/close must toggle the `open`
  // property, otherwise a closed <dialog>'s descendants stay out of the
  // accessibility tree and getByRole("button", ...) cannot find them.
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

// Capture toast calls so we can assert validation and success/error feedback
// without rendering the real toaster.
const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: { success: toastSuccess, error: toastError },
}));

import { server } from "../mocks/server";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const DEVICE_IDS = ["d-1", "d-2"];

const DYNAMIC_TEMPLATE = {
  id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  name: "Ubuntu VM",
  template_type: "dynamic",
};

function mockDynamicTemplates(items: Array<Record<string, unknown>> = [DYNAMIC_TEMPLATE]) {
  server.use(
    http.get("/api/inventory/templates", () =>
      HttpResponse.json({ items, total: items.length, skip: 0, limit: 500 }),
    ),
  );
}

function fillTimes(start: string, end: string) {
  fireEvent.change(screen.getByLabelText("Start time"), { target: { value: start } });
  fireEvent.change(screen.getByLabelText("End time"), { target: { value: end } });
}

async function addDynamicEntry() {
  const addButton = screen.getByRole("button", { name: "Add dynamic instance" });
  // The button stays disabled until the dynamic-template fetch resolves.
  await waitFor(() => expect(addButton).toBeEnabled());
  fireEvent.click(addButton);
}

beforeEach(() => {
  toastSuccess.mockClear();
  toastError.mockClear();
});

describe("CreateReservationModal", () => {
  it("renders the title, selected device count, and form fields", () => {
    renderWithProviders(
      <CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />,
    );

    expect(screen.getByText("Create Reservation")).toBeInTheDocument();
    expect(screen.getByText("2 devices selected")).toBeInTheDocument();
    expect(screen.getByLabelText("Start time")).toBeInTheDocument();
    expect(screen.getByLabelText("End time")).toBeInTheDocument();
    expect(screen.getByLabelText("Purpose (optional)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
  });

  it("singularizes the device count label for a single device", () => {
    renderWithProviders(
      <CreateReservationModal open deviceIds={["only-1"]} onClose={() => {}} />,
    );
    expect(screen.getByText("1 device selected")).toBeInTheDocument();
  });

  it("rejects an end time that is not after the start time", async () => {
    let posted = false;
    server.use(
      http.post("/api/reservations/", () => {
        posted = true;
        return HttpResponse.json({ id: "r-1" });
      }),
    );

    const onClose = vi.fn();
    renderWithProviders(
      <CreateReservationModal open deviceIds={DEVICE_IDS} onClose={onClose} />,
    );

    // End equal to start is invalid (must be strictly after).
    fillTimes("2026-06-01T10:00", "2026-06-01T10:00");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("End time must be after start time"),
    );
    expect(posted).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("posts the reservation, toasts success, and closes on a valid submit", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/reservations/", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "r-1" });
      }),
    );

    const onClose = vi.fn();
    renderWithProviders(
      <CreateReservationModal
        open
        deviceIds={DEVICE_IDS}
        topologyId="topo-9"
        onClose={onClose}
      />,
    );

    fillTimes("2026-06-01T10:00", "2026-06-01T12:00");
    fireEvent.change(screen.getByLabelText("Purpose (optional)"), {
      target: { value: "fw upgrade" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Reservation created"));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(body).toMatchObject({
      device_ids: DEVICE_IDS,
      topology_id: "topo-9",
      purpose: "fw upgrade",
    });
    const sent = body as Record<string, unknown> | null;
    expect(sent?.start_time).toEqual(expect.any(String));
    expect(sent?.end_time).toEqual(expect.any(String));
    // A device-only booking keeps the pre-dynamic wire shape: the key is
    // omitted entirely, not sent as an empty array.
    expect(sent).not.toHaveProperty("dynamic_requests");
  });

  it("surfaces the server error detail when creation fails", async () => {
    server.use(
      http.post("/api/reservations/", () =>
        HttpResponse.json({ detail: "device already reserved" }, { status: 409 }),
      ),
    );

    const onClose = vi.fn();
    renderWithProviders(
      <CreateReservationModal open deviceIds={DEVICE_IDS} onClose={onClose} />,
    );

    fillTimes("2026-06-01T10:00", "2026-06-01T12:00");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("device already reserved"),
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it("invokes onClose when the Cancel button is clicked", () => {
    const onClose = vi.fn();
    renderWithProviders(
      <CreateReservationModal open deviceIds={DEVICE_IDS} onClose={onClose} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("disables Create when there are no devices and no dynamic requests", async () => {
    mockDynamicTemplates();
    renderWithProviders(
      <CreateReservationModal open deviceIds={[]} onClose={() => {}} />,
    );

    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    // Adding a dynamic entry satisfies the device-or-dynamic predicate.
    await addDynamicEntry();
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();

    // Removing it disables submit again.
    fireEvent.click(screen.getByRole("button", { name: "Remove dynamic request 1" }));
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("submits a dynamic-only booking with empty device_ids and expanded dynamic_requests", async () => {
    mockDynamicTemplates();
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/reservations/", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "r-2" });
      }),
    );

    const onClose = vi.fn();
    renderWithProviders(
      <CreateReservationModal open deviceIds={[]} onClose={onClose} />,
    );

    await addDynamicEntry();
    fireEvent.change(screen.getByLabelText("Instance count 1"), { target: { value: "2" } });
    fillTimes("2026-06-01T10:00", "2026-06-01T12:00");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Reservation created"));
    expect(onClose).toHaveBeenCalledTimes(1);
    // A count of 2 expands into two repeated {template_id} items; the backend
    // reads N repetitions as a request for N instances.
    expect(body).toMatchObject({
      device_ids: [],
      dynamic_requests: [
        { template_id: DYNAMIC_TEMPLATE.id },
        { template_id: DYNAMIC_TEMPLATE.id },
      ],
    });
  });

  it("submits a mixed booking with both device_ids and dynamic_requests", async () => {
    mockDynamicTemplates();
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/reservations/", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "r-3" });
      }),
    );

    renderWithProviders(
      <CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />,
    );

    await addDynamicEntry();
    fillTimes("2026-06-01T10:00", "2026-06-01T12:00");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Reservation created"));
    expect(body).toMatchObject({
      device_ids: DEVICE_IDS,
      dynamic_requests: [{ template_id: DYNAMIC_TEMPLATE.id }],
    });
  });

  it("clamps the instance count to the 1..50 backend bounds", async () => {
    mockDynamicTemplates();
    renderWithProviders(
      <CreateReservationModal open deviceIds={[]} onClose={() => {}} />,
    );

    await addDynamicEntry();
    const count = screen.getByLabelText("Instance count 1");

    fireEvent.change(count, { target: { value: "999" } });
    expect(count).toHaveValue(50);

    fireEvent.change(count, { target: { value: "0" } });
    expect(count).toHaveValue(1);
  });

  it("disables Create and warns when the total dynamic requests exceed 50", async () => {
    mockDynamicTemplates();
    renderWithProviders(
      <CreateReservationModal open deviceIds={[]} onClose={() => {}} />,
    );

    // Two entries of 50 and 1: each is within the per-entry clamp, but the
    // total of 51 exceeds the backend's dynamic_requests max_length of 50.
    await addDynamicEntry();
    fireEvent.change(screen.getByLabelText("Instance count 1"), { target: { value: "50" } });
    await addDynamicEntry();

    expect(screen.getByRole("alert")).toHaveTextContent(
      "A reservation can include at most 50 dynamic instances",
    );
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    // Dropping back under the cap clears the warning and re-enables submit.
    fireEvent.change(screen.getByLabelText("Instance count 1"), { target: { value: "49" } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("disables the add button and shows a hint when no dynamic templates exist", async () => {
    mockDynamicTemplates([]);
    renderWithProviders(
      <CreateReservationModal open deviceIds={DEVICE_IDS} onClose={() => {}} />,
    );

    await waitFor(() =>
      expect(screen.getByText("No dynamic templates available")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Add dynamic instance" })).toBeDisabled();
  });
});
