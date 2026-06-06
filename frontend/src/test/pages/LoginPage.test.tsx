import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { mockNavigate, toastError, toastSuccess } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

import { server } from "../mocks/server";
import { LoginPage } from "@/pages/LoginPage";

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
  mockNavigate.mockClear();
  toastError.mockClear();
  toastSuccess.mockClear();
});

describe("LoginPage", () => {
  it("renders the email, password, and submit controls", () => {
    renderWithProviders(<LoginPage />);
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    const pw = screen.getByLabelText("Password") as HTMLInputElement;
    expect(pw).toBeInTheDocument();
    expect(pw.placeholder).toBe("password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows the unconfigured banner and disables fields when configured=false", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: false }),
      ),
    );
    renderWithProviders(<LoginPage />);
    await waitFor(() =>
      expect(
        screen.getByText(/System requires initial configuration/i),
      ).toBeInTheDocument(),
    );
    expect(screen.getByLabelText("Email")).toBeDisabled();
    expect(screen.getByLabelText("Password")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("navigates to /topology on successful login", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true }),
      ),
      http.post("/api/auth/login", () =>
        HttpResponse.json({
          access_token: "a",
          refresh_token: "r",
          user: { id: "u1", email: "u@test", username: "u", role: "user" },
        }),
      ),
    );
    renderWithProviders(<LoginPage />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/topology"));
  });

  it("toasts an error when login is rejected", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true }),
      ),
      http.post("/api/auth/login", () =>
        HttpResponse.json({ detail: "bad creds" }, { status: 401 }),
      ),
    );
    renderWithProviders(<LoginPage />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Invalid email or password"),
    );
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
