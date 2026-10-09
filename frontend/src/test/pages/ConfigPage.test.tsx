import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

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

// Issue #1126: the config login's attempt limit (OPS-CONFIG-22) answers 429
// with Retry-After; the form says so and holds Sign in for the wait, while a
// 401 keeps the wrong-password wording.
describe("ConfigPage login attempt limit (issue #1126)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function submitWrongPassword() {
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  function lockedLogin(headers: Record<string, string>) {
    let calls = 0;
    server.use(
      http.post("/api/config/login", () => {
        calls += 1;
        return HttpResponse.json(
          { detail: "Too many failed login attempts; try again later" },
          { status: 429, headers },
        );
      }),
    );
    return () => calls;
  }

  it("keeps the wrong-password wording for a 401 and leaves Sign in enabled", async () => {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ detail: "Invalid password" }, { status: 401 }),
      ),
    );
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Invalid config password"));
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("shows the wait from Retry-After and disables Sign in until it passes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = lockedLogin({ "Retry-After": "30" });
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Too many failed login attempts; try again in 30 seconds",
      ),
    );
    expect(toastError).not.toHaveBeenCalledWith("Invalid config password");
    const button = screen.getByRole("button", { name: "Sign in" });
    await waitFor(() => expect(button).toBeDisabled());

    // A submit inside the wait (Enter in the field) sends nothing.
    fireEvent.submit(button.closest("form") as HTMLFormElement);
    await vi.advanceTimersByTimeAsync(29_000);
    expect(button).toBeDisabled();
    expect(calls()).toBe(1);

    await vi.advanceTimersByTimeAsync(1_000);
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("uses the singular for a one-second wait", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    lockedLogin({ "Retry-After": "1" });
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Too many failed login attempts; try again in 1 second",
      ),
    );
    await vi.advanceTimersByTimeAsync(1_000);
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled());
  });

  it("shows the server's sentence and keeps Sign in enabled when Retry-After is absent", async () => {
    lockedLogin({});
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Too many failed login attempts; try again later"),
    );
    expect(toastError).not.toHaveBeenCalledWith("Invalid config password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("treats a Retry-After that is not a whole number of seconds as no stated wait", async () => {
    lockedLogin({ "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT" });
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Too many failed login attempts; try again later"),
    );
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
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
