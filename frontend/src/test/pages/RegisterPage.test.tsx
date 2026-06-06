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
import { RegisterPage } from "@/pages/RegisterPage";

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

describe("RegisterPage", () => {
  it("renders email, username, password, and submit", () => {
    renderWithProviders(<RegisterPage />);
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    const pw = screen.getByLabelText("Password") as HTMLInputElement;
    expect(pw.placeholder).toBe("password");
    expect(screen.getByRole("button", { name: "Create account" })).toBeInTheDocument();
  });

  it("navigates to /login and toasts on success", async () => {
    server.use(
      http.post("/api/auth/register", () =>
        HttpResponse.json({
          id: "u1",
          email: "u@test",
          username: "u",
          role: "user",
        }),
      ),
    );
    renderWithProviders(<RegisterPage />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "u" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        "Account created. Please sign in.",
      ),
    );
    expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  it("toasts the backend detail on failure", async () => {
    server.use(
      http.post("/api/auth/register", () =>
        HttpResponse.json({ detail: "Email already in use" }, { status: 400 }),
      ),
    );
    renderWithProviders(<RegisterPage />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "u" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Email already in use"),
    );
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
