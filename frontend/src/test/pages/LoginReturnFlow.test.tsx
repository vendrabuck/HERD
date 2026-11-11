import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("react-hot-toast", () => ({ default: { error: vi.fn(), success: vi.fn() } }));

import { server } from "../mocks/server";
import { AuthGuard, GuestGuard } from "@/components/guards";
import { LoginPage } from "@/pages/LoginPage";
import apiClient from "@/api/client";
import { useAuthStore } from "@/stores/authStore";

// Issue #1333, through the real guards, the real login page, and the real 401
// interceptor: a deep link and an expired session both come back to the page
// the user was on after signing in.

function Where() {
  const location = useLocation();
  return (
    <div>
      <p>At {location.pathname + location.search + location.hash}</p>
      <button type="button" onClick={() => apiClient.get("/thing").catch(() => {})}>
        Load
      </button>
    </div>
  );
}

function renderApp(initial: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route
            path="/login"
            element={
              <GuestGuard>
                <LoginPage />
              </GuestGuard>
            }
          />
          <Route
            path="*"
            element={
              <AuthGuard>
                <Where />
              </AuthGuard>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function signIn() {
  fireEvent.change(await screen.findByLabelText("Email"), {
    target: { value: "u@test" },
  });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

beforeEach(() => {
  localStorage.clear();
  useAuthStore.setState({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
  server.use(
    http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
    http.post("/api/auth/login", () =>
      HttpResponse.json({ access_token: "T9", refresh_token: "R9" }),
    ),
  );
});

describe("login return path through the guards (issue #1333)", () => {
  it("a signed-out deep link returns to that page after sign-in", async () => {
    renderApp("/topology/abc?tab=wiring#n1");
    await signIn();
    expect(await screen.findByText("At /topology/abc?tab=wiring#n1")).toBeInTheDocument();
  });

  it("a session that expires mid-page (refresh refused) returns to that page after sign-in", { timeout: 10000 }, async () => {
    useAuthStore.getState().setTokens("T1", "R1");
    // A lock keeps the refusal immediate (no unlocked grace wait).
    Object.defineProperty(navigator, "locks", {
      value: {
        request: (_n: string, _o: unknown, cb: () => Promise<unknown>) => cb(),
      },
      configurable: true,
    });
    server.use(
      http.get("/api/thing", ({ request }) =>
        request.headers.get("Authorization") === "Bearer T9"
          ? HttpResponse.json({ ok: true })
          : HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
      http.post("/api/auth/refresh", () =>
        HttpResponse.json({ detail: "Invalid or expired refresh token" }, { status: 401 }),
      ),
    );
    try {
      renderApp("/reservations/r-1?view=wiring");
      expect(screen.getByText("At /reservations/r-1?view=wiring")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Load" }));
      // The refusal is final only after the refusal grace (REFUSAL_GRACE_MS, 2 s).
      await waitFor(
        () => expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument(),
        { timeout: 5000 },
      );
      await signIn();
      expect(await screen.findByText("At /reservations/r-1?view=wiring")).toBeInTheDocument();
    } finally {
      delete (navigator as unknown as { locks?: unknown }).locks;
    }
  });

  it("a recorded //evil.example never leaves the app", async () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[{ pathname: "/login", state: { from: "//evil.example" } }]}>
          <Routes>
            <Route
              path="/login"
              element={
                <GuestGuard>
                  <LoginPage />
                </GuestGuard>
              }
            />
            <Route path="*" element={<Where />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await signIn();
    expect(await screen.findByText("At /topology")).toBeInTheDocument();
  });
});
