import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, beforeEach } from "vitest";

import { server } from "../mocks/server";
import { useLogin, useLogout, useCurrentUser } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("auth api hooks", () => {
  beforeEach(() => {
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
    });
  });

  it("useLogin stores tokens from the response", async () => {
    server.use(
      http.post("/api/auth/login", () =>
        HttpResponse.json({
          access_token: "access-123",
          refresh_token: "refresh-456",
          token_type: "bearer",
        }),
      ),
    );
    const { result } = renderHook(() => useLogin(), { wrapper });
    result.current.mutate({ email: "u@test.com", password: "password" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(useAuthStore.getState().accessToken).toBe("access-123");
    expect(useAuthStore.getState().refreshToken).toBe("refresh-456");
  });

  it("useLogin surfaces server errors and leaves tokens untouched", async () => {
    server.use(
      http.post("/api/auth/login", () =>
        HttpResponse.json({ detail: "bad creds" }, { status: 401 }),
      ),
    );
    const { result } = renderHook(() => useLogin(), { wrapper });
    result.current.mutate({ email: "u@test.com", password: "bad" });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(useAuthStore.getState().accessToken).toBeNull();
  });

  it("useCurrentUser is disabled when not authenticated", () => {
    const { result } = renderHook(() => useCurrentUser(), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useCurrentUser fetches the authenticated user", async () => {
    useAuthStore.getState().setTokens("tok", "ref");
    server.use(
      http.get("/api/auth/me", () =>
        HttpResponse.json({
          id: "u-1",
          email: "u@test.com",
          username: "u",
          role: "user",
          is_active: true,
        }),
      ),
    );
    const { result } = renderHook(() => useCurrentUser(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.username).toBe("u");
  });

  it("useLogout clears auth even when the server errors", async () => {
    useAuthStore.getState().setTokens("tok", "ref");
    server.use(
      http.post("/api/auth/logout", () =>
        HttpResponse.json({ detail: "already revoked" }, { status: 400 }),
      ),
    );
    const { result } = renderHook(() => useLogout(), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });
});
