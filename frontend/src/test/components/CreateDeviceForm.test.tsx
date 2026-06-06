import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

// The dynamic field renderer is exercised by its own test. Stub it so this test
// stays focused on the form's own state, validation, and submit behavior; the
// stub also lets us assert that it only appears once a template is selected.
vi.mock("@/components/devices/DynamicFieldRenderer", () => ({
  DynamicFieldRenderer: ({ sections }: { sections: unknown[] }) => (
    <div data-testid="dynamic-fields">{sections.length} sections</div>
  ),
}));

import { server } from "../mocks/server";
import { CreateDeviceForm } from "@/components/admin/CreateDeviceForm";

const TEMPLATE = {
  id: "tmpl-1",
  name: "Firewall template",
  template_type: "device",
  driver_id: null,
  driver_name: null,
  connection_type: null,
  exclusive: false,
  icon: null,
  description: null,
  vendor: "acme",
  model: "fw-1",
  part_number: null,
  sections: [{ name: "General", fields: [] }],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  poll_interval_seconds: null,
};

function templatesHandler(items: unknown[]) {
  return http.get("/api/inventory/templates", () =>
    HttpResponse.json({ items, total: items.length, skip: 0, limit: 500 }),
  );
}

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
  server.use(templatesHandler([]));
});

describe("CreateDeviceForm", () => {
  it("renders the core fields with the default template placeholder option", () => {
    renderWithProviders(<CreateDeviceForm />);
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
    expect(screen.getByLabelText("Template")).toBeInTheDocument();
    expect(screen.getByLabelText("Topology type")).toBeInTheDocument();
    expect(screen.getByLabelText("Status")).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Select a template..." }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Add Device" }),
    ).toBeInTheDocument();
  });

  it("populates the template dropdown from the fetched list", async () => {
    server.use(templatesHandler([TEMPLATE]));
    renderWithProviders(<CreateDeviceForm />);
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "Firewall template" }),
      ).toBeInTheDocument(),
    );
  });

  it("does not render the dynamic field section until a template is selected", async () => {
    server.use(templatesHandler([TEMPLATE]));
    renderWithProviders(<CreateDeviceForm />);
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "Firewall template" }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("dynamic-fields")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Template"), {
      target: { value: "tmpl-1" },
    });
    expect(screen.getByTestId("dynamic-fields")).toHaveTextContent("1 sections");
  });

  it("blocks submit and toasts when no template is selected", async () => {
    let posted = false;
    server.use(
      templatesHandler([TEMPLATE]),
      http.post("/api/inventory/devices", () => {
        posted = true;
        return HttpResponse.json({}, { status: 201 });
      }),
    );
    renderWithProviders(<CreateDeviceForm />);

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "fw-a" },
    });
    fireEvent.submit(screen.getByRole("button", { name: "Add Device" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Select a template"),
    );
    expect(posted).toBe(false);
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("rejects a poll interval below the 30 second minimum without posting", async () => {
    let posted = false;
    server.use(
      templatesHandler([TEMPLATE]),
      http.post("/api/inventory/devices", () => {
        posted = true;
        return HttpResponse.json({}, { status: 201 });
      }),
    );
    renderWithProviders(<CreateDeviceForm />);
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "Firewall template" }),
      ).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "fw-a" },
    });
    fireEvent.change(screen.getByLabelText("Template"), {
      target: { value: "tmpl-1" },
    });
    fireEvent.change(
      screen.getByLabelText("Health-poll interval (seconds)"),
      { target: { value: "10" } },
    );
    fireEvent.submit(screen.getByRole("button", { name: "Add Device" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Poll interval must be a whole number >= 30 (or blank to inherit)",
      ),
    );
    expect(posted).toBe(false);
  });

  it("posts the device, sends a null poll interval when blank, and resets on success", async () => {
    let received: Record<string, unknown> | undefined;
    server.use(
      templatesHandler([TEMPLATE]),
      http.post("/api/inventory/devices", async ({ request }) => {
        received = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          { id: "dev-1", name: received.name },
          { status: 201 },
        );
      }),
    );
    renderWithProviders(<CreateDeviceForm />);
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "Firewall template" }),
      ).toBeInTheDocument(),
    );

    const nameInput = screen.getByLabelText("Name") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "fw-a" } });
    fireEvent.change(screen.getByLabelText("Template"), {
      target: { value: "tmpl-1" },
    });
    fireEvent.submit(screen.getByRole("button", { name: "Add Device" }));

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Device created"),
    );
    expect(received).toMatchObject({
      name: "fw-a",
      template_id: "tmpl-1",
      topology_type: "PHYSICAL",
      status: "AVAILABLE",
      poll_interval_seconds: null,
    });
    // The form resets to its initial state on success.
    expect(nameInput.value).toBe("");
  });

  it("surfaces the server detail message when creation fails", async () => {
    server.use(
      templatesHandler([TEMPLATE]),
      http.post("/api/inventory/devices", () =>
        HttpResponse.json({ detail: "name already in use" }, { status: 409 }),
      ),
    );
    renderWithProviders(<CreateDeviceForm />);
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "Firewall template" }),
      ).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "fw-a" },
    });
    fireEvent.change(screen.getByLabelText("Template"), {
      target: { value: "tmpl-1" },
    });
    fireEvent.submit(screen.getByRole("button", { name: "Add Device" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("name already in use"),
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });
});
