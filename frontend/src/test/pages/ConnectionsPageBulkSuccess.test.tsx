import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

// Split out from ConnectionsPage.test.tsx because it needs to mock
// MultiConnectDialog directly to drive its onSuccess callback, which that
// file's module-scoped setup does not do (it exercises the real dialog).

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

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

// Minimal stand-in exposing only the two buttons ConnectionsPage's wiring
// depends on: submit (calls onSubmit then onSuccess, mirroring the real
// dialog's own success path) and cancel.
const mockOnSubmit = vi.fn();
vi.mock("@/components/admin/connections/MultiConnectDialog", () => ({
  MultiConnectDialog: ({
    onSubmit,
    onSuccess,
    onCancel,
  }: {
    onSubmit: (items: unknown[]) => Promise<{ created: number }>;
    onSuccess: (created: number) => void;
    onCancel: () => void;
  }) => (
    <div role="dialog" aria-label="mock-multi-connect">
      <button
        onClick={async () => {
          mockOnSubmit();
          const result = await onSubmit([{ fake: "item" }]);
          onSuccess(result.created);
        }}
      >
        mock-submit
      </button>
      <button onClick={onCancel}>mock-cancel</button>
    </div>
  ),
}));

import { http, HttpResponse } from "msw";
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

beforeEach(() => {
  toastSuccess.mockClear();
  toastError.mockClear();
  mockOnSubmit.mockClear();
  server.use(
    http.get("/api/inventory/devices", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
    http.get("/api/cabling/connections", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 }),
    ),
  );
});

describe("ConnectionsPage bulk-create success wiring", () => {
  it("posts the bulk items through createConnectionsBulk and shows a pluralized toast on success", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/cabling/connections/bulk", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({ created: 3, results: [] }, { status: 200 });
      }),
    );
    renderWithProviders(<ConnectionsPage />);
    await waitFor(() => expect(screen.getByText("No connections found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
    fireEvent.click(await screen.findByText("mock-submit"));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Created 3 connections"));
    expect(captured).toEqual({ items: [{ fake: "item" }] });
    // The dialog closes on success.
    expect(screen.queryByText("mock-submit")).not.toBeInTheDocument();
  });

  it("uses the singular form for exactly one created connection", async () => {
    server.use(
      http.post("/api/cabling/connections/bulk", () =>
        HttpResponse.json({ created: 1, results: [] }, { status: 200 }),
      ),
    );
    renderWithProviders(<ConnectionsPage />);
    await waitFor(() => expect(screen.getByText("No connections found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
    fireEvent.click(await screen.findByText("mock-submit"));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Created 1 connection"));
  });

  it("cancelling the multi-connect dialog closes it without creating anything", async () => {
    renderWithProviders(<ConnectionsPage />);
    await waitFor(() => expect(screen.getByText("No connections found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
    fireEvent.click(await screen.findByText("mock-cancel"));

    expect(screen.queryByText("mock-cancel")).not.toBeInTheDocument();
    expect(mockOnSubmit).not.toHaveBeenCalled();
    expect(toastSuccess).not.toHaveBeenCalled();
  });
});
