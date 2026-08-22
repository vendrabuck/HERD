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
});
