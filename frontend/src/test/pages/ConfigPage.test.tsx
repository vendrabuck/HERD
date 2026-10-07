import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

import { server } from "../mocks/server";
import { ConfigPage } from "@/pages/ConfigPage";
import { useConfigStore } from "@/stores/configStore";
import { TOAST_CLEARANCE_CLASS } from "@/lib/toastClearance";

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
  // Each test starts with no config token in the store.
  useConfigStore.getState().clearConfigToken();
});

describe("ConfigPage (unauthenticated)", () => {
  it("renders the config login form when no token is present", () => {
    renderWithProviders(<ConfigPage />);
    expect(screen.getByText("HERD Configuration")).toBeInTheDocument();
    const pw = screen.getByLabelText("Config Password") as HTMLInputElement;
    expect(pw.placeholder).toBe("password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("Back to login")).toBeInTheDocument();
  });

  it("posts the password and stores the token on success", async () => {
    let captured: { password?: string } = {};
    server.use(
      http.post("/api/config/login", async ({ request }) => {
        captured = (await request.json()) as { password?: string };
        return HttpResponse.json({ token: "ct", password_changed: true });
      }),
    );
    renderWithProviders(<ConfigPage />);
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(useConfigStore.getState().configToken).toBe("ct"),
    );
    expect(captured.password).toBe("password");
  });

  it("toasts an error when the config login is rejected", async () => {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ detail: "bad" }, { status: 401 }),
      ),
    );
    renderWithProviders(<ConfigPage />);
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Invalid config password"),
    );
    expect(useConfigStore.getState().configToken).toBeNull();
  });
});

describe("ConfigPage (editor)", () => {
  // Issue #988: the editor ends in Save, Save and Restart, and Back to login at
  // the bottom centre, where toasts appear; the page carries the bottom padding
  // that lets them scroll clear of a stack of three toasts.
  it("pads the page end so the last controls can scroll clear of the toasts (issue #988)", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true, password_changed: true }),
      ),
      http.get("/api/config/schema", () =>
        HttpResponse.json({
          fields: [
            {
              key: "LOG_LEVEL",
              label: "Log level",
              type: "string",
              required: false,
              group: "General",
              secret: false,
              description: "",
            },
          ],
        }),
      ),
      http.get("/api/config/settings", () => HttpResponse.json({ values: { LOG_LEVEL: "INFO" } })),
    );
    useConfigStore.getState().setConfigToken("ct");
    renderWithProviders(<ConfigPage />);
    const save = await screen.findByRole("button", { name: "Save and Restart" });
    const page = save.closest(".min-h-screen") as HTMLElement;
    expect(page.className.split(/\s+/)).toContain(TOAST_CLEARANCE_CLASS);
  });
});
