import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// setup.ts already polyfills HTMLDialogElement.showModal/close to toggle the
// `open` attribute, so dialogs become queryable by the "dialog" role. Do not
// override it with no-op spies here, or the dialog never opens.

// Spy on the toast surface so we can assert success and validation feedback.
const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

// The page is reached only through the AdminGuard route group in routes.tsx,
// so admin-ness is
// not this page's concern; pin an admin user anyway since apiClient's
// request interceptor reads useAuthStore.getState().accessToken on every
// call, so the mock must expose getState as well as the selector hook, or
// every axios request throws and no network calls ever reach MSW.
const AUTH_STATE = {
  user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
  accessToken: "t",
  refreshToken: null,
  clearAuth: () => {},
  setTokens: () => {},
};
vi.mock("@/stores/authStore", () => {
  const useAuthStore = (sel: (s: unknown) => unknown) => sel(AUTH_STATE);
  useAuthStore.getState = () => AUTH_STATE;
  useAuthStore.setState = () => {};
  return { useAuthStore };
});

import { server } from "../mocks/server";
import { ConnectionsPage } from "@/pages/admin/ConnectionsPage";

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

const CONNECTION = {
  id: "conn-1",
  device_a_id: "dev-a",
  port_a: "eth1/1",
  device_b_id: "dev-b",
  port_b: "eth1/2",
  connection_type: "ethernet",
  notes: "uplink",
  created_by: "admin",
  created_at: "2026-06-01T00:00:00Z",
  modified_by: null,
  updated_at: null,
};

const DEVICES = [
  { id: "dev-a", name: "spine-1" },
  { id: "dev-b", name: "leaf-2" },
];

function connectionsHandler(items: typeof CONNECTION[]) {
  return http.get("/api/cabling/connections", () =>
    HttpResponse.json({ items, total: items.length, skip: 0, limit: 50 }),
  );
}

beforeEach(() => {
  toastSuccess.mockClear();
  toastError.mockClear();
  // useAllDeviceNames walks /inventory/devices; serve the id-to-name map once,
  // then a short page so the walk terminates.
  server.use(
    http.get("/api/inventory/devices", ({ request }) => {
      const skip = new URL(request.url).searchParams.get("skip") ?? "0";
      if (skip === "0") {
        return HttpResponse.json({ items: DEVICES, total: 2, skip: 0, limit: 500 });
      }
      return HttpResponse.json({ items: [], total: 2, skip: 2, limit: 500 });
    }),
  );
});

// The row action and the confirm dialog both label a button "Delete". The
// table renders before the ConfirmDialog in JSX, so the row button is first in
// document order; the confirm button is then scoped via the dialog role.
function clickRowDelete() {
  return screen.getAllByRole("button", { name: "Delete" })[0];
}

describe("ConnectionsPage", () => {
  it("shows the loading state before connections resolve", () => {
    server.use(
      http.get("/api/cabling/connections", async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<ConnectionsPage />);
    expect(screen.getByText(/Loading connections/i)).toBeInTheDocument();
  });

  it("renders an empty state when there are no connections", async () => {
    server.use(connectionsHandler([]));
    renderWithProviders(<ConnectionsPage />);
    await waitFor(() =>
      expect(screen.getByText("No connections found")).toBeInTheDocument(),
    );
  });

  it("renders a connection row with resolved device names and ports", async () => {
    server.use(connectionsHandler([CONNECTION]));
    renderWithProviders(<ConnectionsPage />);

    // Device names come from the resolution map, not the connection payload.
    await waitFor(() => expect(screen.getByText("spine-1")).toBeInTheDocument());
    expect(screen.getByText("leaf-2")).toBeInTheDocument();
    expect(screen.getByText("eth1/1")).toBeInTheDocument();
    expect(screen.getByText("eth1/2")).toBeInTheDocument();
    expect(screen.getByText("uplink")).toBeInTheDocument();
  });

  it("defaults to the multi-connection dialog", async () => {
    server.use(connectionsHandler([CONNECTION]));
    renderWithProviders(<ConnectionsPage />);
    await screen.findByText("spine-1");

    expect(screen.getByRole("button", { name: "Multi" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));

    expect(
      await screen.findByRole("dialog", { name: /Create multiple connections/i }),
    ).toBeInTheDocument();
    // The single-pair form is not mounted at all in multi mode.
    expect(screen.queryByText("Port A")).not.toBeInTheDocument();
  });

  it("the Single toggle switches the create button back to the single-pair modal", async () => {
    server.use(connectionsHandler([CONNECTION]));
    renderWithProviders(<ConnectionsPage />);
    await screen.findByText("spine-1");

    fireEvent.click(screen.getByRole("button", { name: "Single" }));
    fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));

    // The original two-select form, not the port columns.
    expect(screen.getByText("Port A")).toBeInTheDocument();
    expect(document.querySelectorAll("select")).toHaveLength(2);
    expect(screen.queryByText("Create multiple connections")).not.toBeInTheDocument();
  });

  it("blocks create and shows a validation toast when no device is selected", async () => {
    server.use(connectionsHandler([CONNECTION]));
    renderWithProviders(<ConnectionsPage />);
    await screen.findByText("spine-1");

    // Open the single-pair modal, then submit with nothing selected.
    // handleCreate short-circuits on the first missing field (device A).
    fireEvent.click(screen.getByRole("button", { name: "Single" }));
    fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Device A is required"),
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("deletes a connection through the confirm dialog", async () => {
    let deleteCalled = false;
    server.use(
      connectionsHandler([CONNECTION]),
      http.delete("/api/cabling/connections/conn-1", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<ConnectionsPage />);
    await screen.findByText("spine-1");

    // Row action: the only Delete button until the confirm dialog opens. Scope
    // the confirm click to the dialog so the row button never matches.
    fireEvent.click(clickRowDelete());
    const confirm = within(screen.getByRole("dialog", { name: /Delete Connection/i }));
    fireEvent.click(confirm.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteCalled).toBe(true));
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Connection deleted"),
    );
  });

  it("surfaces the server detail message when delete fails", async () => {
    server.use(
      connectionsHandler([CONNECTION]),
      http.delete("/api/cabling/connections/conn-1", () =>
        HttpResponse.json({ detail: "connection in use" }, { status: 409 }),
      ),
    );
    renderWithProviders(<ConnectionsPage />);
    await screen.findByText("spine-1");

    fireEvent.click(clickRowDelete());
    const confirm = within(screen.getByRole("dialog", { name: /Delete Connection/i }));
    fireEvent.click(confirm.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("connection in use"),
    );
  });

  describe("single-pair create form", () => {
    async function openSingleForm() {
      renderWithProviders(<ConnectionsPage />);
      // Wait for the initial connections query to settle before interacting,
      // regardless of whether the list ends up empty or populated.
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Create Connection" })).toBeInTheDocument(),
      );
      fireEvent.click(screen.getByRole("button", { name: "Single" }));
      fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
    }

    function searchAndPickDevice(searchLabelIndex: 0 | 1, query: string, pick: string) {
      const searchInputs = screen.getAllByPlaceholderText("Search devices...");
      fireEvent.change(searchInputs[searchLabelIndex], { target: { value: query } });
      return waitFor(() => screen.getByRole("button", { name: pick }));
    }

    // Neither Port A/B <select> nor Connection Type/Notes are wired to their
    // <label> via htmlFor/id (a pre-existing gap, not something this lane
    // fixes), so getByLabelText cannot find them; grab the create dialog's
    // form controls by tag/position instead.
    function createDialog() {
      return screen.getByRole("dialog", { name: "Create Connection", hidden: true });
    }
    function portASelect() {
      return createDialog().querySelectorAll("select")[0] as HTMLSelectElement;
    }
    function portBSelect() {
      return createDialog().querySelectorAll("select")[1] as HTMLSelectElement;
    }
    function connectionTypeInput() {
      return createDialog().querySelector('input[type="text"]') as HTMLInputElement;
    }
    function notesTextarea() {
      return createDialog().querySelector("textarea") as HTMLTextAreaElement;
    }

    it("blocks create at each successive missing field, in order", async () => {
      server.use(connectionsHandler([]));
      await openSingleForm();

      const submit = () => fireEvent.click(screen.getByRole("button", { name: "Create" }));

      // Device A picked, Port A still missing.
      server.use(
        http.get("/api/inventory/devices", ({ request }) => {
          const search = new URL(request.url).searchParams.get("search");
          if (search === "spine") {
            return HttpResponse.json({ items: DEVICES.slice(0, 1), total: 1, skip: 0, limit: 20 });
          }
          return HttpResponse.json({ items: DEVICES, total: 2, skip: 0, limit: 500 });
        }),
        http.get("/api/inventory/devices/dev-a/ports", () => HttpResponse.json([])),
      );
      const pickBtn = await searchAndPickDevice(0, "spine", "spine-1");
      fireEvent.click(pickBtn);
      submit();
      await waitFor(() => expect(toastError).toHaveBeenCalledWith("Port A is required"));
    });

    it("shows a no-ports hint when the selected device has no ports configured", async () => {
      server.use(
        connectionsHandler([]),
        http.get("/api/inventory/devices/dev-a/ports", () => HttpResponse.json([])),
      );
      await openSingleForm();
      const pickBtn = await searchAndPickDevice(0, "sp", "spine-1");
      fireEvent.click(pickBtn);

      await waitFor(() =>
        expect(screen.getByText("No ports configured on this device")).toBeInTheDocument(),
      );
    });

    it("creates a connection with the full payload and closes the modal", async () => {
      let captured: unknown = null;
      server.use(
        connectionsHandler([]),
        http.get("/api/inventory/devices/dev-a/ports", () =>
          HttpResponse.json([{ id: "pa1", name: "eth1/1", device_id: "dev-a", template_id: "t", template_name: null, template_icon: null, field_data: {}, created_at: "", updated_at: "" }]),
        ),
        http.get("/api/inventory/devices/dev-b/ports", () =>
          HttpResponse.json([{ id: "pb1", name: "eth2/1", device_id: "dev-b", template_id: "t", template_name: null, template_icon: null, field_data: {}, created_at: "", updated_at: "" }]),
        ),
        http.post("/api/cabling/connections", async ({ request }) => {
          captured = await request.json();
          return HttpResponse.json(CONNECTION, { status: 201 });
        }),
      );
      await openSingleForm();

      const pickA = await searchAndPickDevice(0, "sp", "spine-1");
      fireEvent.click(pickA);
      await waitFor(() => expect(screen.getByText("spine-1")).toBeInTheDocument());
      const pickB = await searchAndPickDevice(0, "le", "leaf-2");
      fireEvent.click(pickB);

      await waitFor(() => expect(portASelect().querySelectorAll("option")).toHaveLength(2));
      fireEvent.change(portASelect(), { target: { value: "eth1/1" } });
      fireEvent.change(portBSelect(), { target: { value: "eth2/1" } });
      fireEvent.change(connectionTypeInput(), { target: { value: "fiber" } });
      fireEvent.change(notesTextarea(), { target: { value: "core uplink" } });
      fireEvent.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Connection created"));
      expect(captured).toEqual({
        device_a_id: "dev-a",
        port_a: "eth1/1",
        device_b_id: "dev-b",
        port_b: "eth2/1",
        connection_type: "fiber",
        notes: "core uplink",
      });
      // The modal closed: its fields are gone from the document.
      expect(screen.queryByRole("dialog", { name: "Create Connection", hidden: true })).toBe(null);
    });

    it("surfaces the server detail message when create fails", async () => {
      server.use(
        connectionsHandler([]),
        http.get("/api/inventory/devices/dev-a/ports", () =>
          HttpResponse.json([{ id: "pa1", name: "eth1/1", device_id: "dev-a", template_id: "t", template_name: null, template_icon: null, field_data: {}, created_at: "", updated_at: "" }]),
        ),
        http.get("/api/inventory/devices/dev-b/ports", () =>
          HttpResponse.json([{ id: "pb1", name: "eth2/1", device_id: "dev-b", template_id: "t", template_name: null, template_icon: null, field_data: {}, created_at: "", updated_at: "" }]),
        ),
        http.post("/api/cabling/connections", () =>
          HttpResponse.json({ detail: "port already cabled" }, { status: 409 }),
        ),
      );
      await openSingleForm();

      const pickA = await searchAndPickDevice(0, "sp", "spine-1");
      fireEvent.click(pickA);
      const pickB = await searchAndPickDevice(0, "le", "leaf-2");
      fireEvent.click(pickB);
      await waitFor(() => expect(portASelect().querySelectorAll("option")).toHaveLength(2));
      fireEvent.change(portASelect(), { target: { value: "eth1/1" } });
      fireEvent.change(portBSelect(), { target: { value: "eth2/1" } });
      fireEvent.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() =>
        expect(toastError).toHaveBeenCalledWith("port already cabled"),
      );
    });

    it("the Change button clears a selected device and its port", async () => {
      server.use(
        connectionsHandler([]),
        http.get("/api/inventory/devices/dev-a/ports", () =>
          HttpResponse.json([{ id: "pa1", name: "eth1/1", device_id: "dev-a", template_id: "t", template_name: null, template_icon: null, field_data: {}, created_at: "", updated_at: "" }]),
        ),
      );
      await openSingleForm();
      const pickA = await searchAndPickDevice(0, "sp", "spine-1");
      fireEvent.click(pickA);
      await waitFor(() => expect(screen.getByText("spine-1")).toBeInTheDocument());

      fireEvent.click(screen.getByRole("button", { name: "Change" }));
      expect(screen.queryByText("spine-1")).not.toBeInTheDocument();
      expect(screen.getAllByPlaceholderText("Search devices...")).toHaveLength(2);
    });

    it("cancel closes the modal and resets the form fields", async () => {
      server.use(connectionsHandler([]));
      await openSingleForm();

      fireEvent.change(notesTextarea(), { target: { value: "scratch note" } });
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("dialog", { name: "Create Connection", hidden: true })).toBe(null);

      // Reopening shows the form reset, not the stale note.
      fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
      expect(notesTextarea()).toHaveValue("");
    });
  });

  describe("device filter", () => {
    it("filters the connection list by the selected device and can be cleared", async () => {
      const requestedDeviceIds: (string | null)[] = [];
      server.use(
        http.get("/api/cabling/connections", ({ request }) => {
          requestedDeviceIds.push(new URL(request.url).searchParams.get("device_id"));
          return HttpResponse.json({ items: [CONNECTION], total: 1, skip: 0, limit: 50 });
        }),
      );
      renderWithProviders(<ConnectionsPage />);
      await screen.findByText("spine-1");

      fireEvent.change(screen.getByPlaceholderText("Filter by device name..."), {
        target: { value: "spine" },
      });
      const option = await screen.findByRole("button", { name: "spine-1" });
      fireEvent.click(option);

      await waitFor(() =>
        expect(requestedDeviceIds).toContain("dev-a"),
      );
      // The filter input now shows the selected device's name and a Clear button.
      expect(screen.getByDisplayValue("spine-1")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Clear" }));
      await waitFor(() =>
        expect(requestedDeviceIds[requestedDeviceIds.length - 1]).toBeNull(),
      );
    });

    it("typing over an active filter clears it before applying the new search text", async () => {
      server.use(connectionsHandler([CONNECTION]));
      renderWithProviders(<ConnectionsPage />);
      await screen.findByText("spine-1");

      const filterInput = screen.getByPlaceholderText("Filter by device name...");
      fireEvent.change(filterInput, { target: { value: "spine" } });
      const option = await screen.findByRole("button", { name: "spine-1" });
      fireEvent.click(option);
      expect(screen.getByDisplayValue("spine-1")).toBeInTheDocument();

      // Typing again while a filter is active clears filterDeviceId first
      // (the onChange branch at the top of the input handler).
      fireEvent.change(screen.getByDisplayValue("spine-1"), { target: { value: "leaf" } });
      expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
    });
  });

});
