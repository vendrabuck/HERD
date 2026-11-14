import { http, HttpResponse } from "msw";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, beforeEach, vi } from "vitest";

import { server } from "../mocks/server";
import {
  LOGIN_OPTIONS_PATH,
  OIDC_EXCHANGE_PATH,
  adoptSignInTokens,
  exchangeOidcHandoff,
  useLogin,
  useLoginOptions,
} from "@/api/auth";
import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, useAuthStore } from "@/stores/authStore";

// ADR 0016 phase 4: the login-options read and the handoff exchange.

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(() => {
  localStorage.clear();
  useAuthStore.setState({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
});

describe("the single sign-on API paths", () => {
  it("are the ADR's routes under the /api base", () => {
    expect(LOGIN_OPTIONS_PATH).toBe("/auth/login-options");
    expect(OIDC_EXCHANGE_PATH).toBe("/auth/oidc/exchange");
  });
});

describe("useLoginOptions", () => {
  it("resolves with the parsed answer", async () => {
    server.use(
      http.get("/api/auth/login-options", () =>
        HttpResponse.json({
          auth_method: "oidc",
          registration_open: false,
          password_login: true,
          sso: { provider_name: "Acme" },
        }),
      ),
    );
    const { result } = renderHook(() => useLoginOptions(), { wrapper: makeWrapper(newClient()) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      auth_method: "oidc",
      registration_open: false,
      password_login: true,
      sso: { provider_name: "Acme" },
    });
  });

  const OPTIONS_FAILURES: Array<[string, () => Response]> = [
    ["a 404 (a backend without the route)", () => HttpResponse.json({ detail: "Not Found" }, { status: 404 })],
    ["a 500", () => HttpResponse.json({ detail: "boom" }, { status: 500 })],
    ["a 502 from the gateway", () => new HttpResponse("Bad Gateway", { status: 502 })],
    ["a network error", () => HttpResponse.error()],
    [
      "malformed JSON",
      () => new HttpResponse("{not json", { status: 200, headers: { "Content-Type": "application/json" } }),
    ],
    ["an HTML page with a 200", () => new HttpResponse("<html></html>", { status: 200 })],
    ["a partial body", () => HttpResponse.json({ password_login: true })],
  ];
  it.each(OPTIONS_FAILURES)("errors on %s, and is not retried", async (_label, answer) => {
    let calls = 0;
    server.use(
      http.get("/api/auth/login-options", () => {
        calls += 1;
        return answer();
      }),
    );
    // A client whose default would retry, so the hook's own retry: false is what is tested.
    const client = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 1 } } });
    const { result } = renderHook(() => useLoginOptions(), { wrapper: makeWrapper(client) });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(calls).toBe(1);
  });

  it("is not refetched when the window regains focus", async () => {
    let calls = 0;
    server.use(
      http.get("/api/auth/login-options", () => {
        calls += 1;
        return HttpResponse.json({
          auth_method: "local",
          registration_open: true,
          password_login: true,
          sso: null,
        });
      }),
    );
    // staleTime 0 (the default): a query that allowed it would refetch on focus.
    const { result } = renderHook(() => useLoginOptions(), { wrapper: makeWrapper(newClient()) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(calls).toBe(1);
    try {
      act(() => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
      await new Promise((resolve) => setTimeout(resolve, 50));
    } finally {
      focusManager.setFocused(undefined);
    }
    expect(calls).toBe(1);
  });

  it("a 401 from it never triggers a token refresh", async () => {
    useAuthStore.getState().setTokens("stale-access", "some-refresh");
    const refresh = vi.fn();
    server.use(
      http.get("/api/auth/login-options", () => HttpResponse.json({ detail: "x" }, { status: 401 })),
      http.post("/api/auth/refresh", () => {
        refresh();
        return HttpResponse.json({ access_token: "n", refresh_token: "n" });
      }),
    );
    const { result } = renderHook(() => useLoginOptions(), { wrapper: makeWrapper(newClient()) });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(refresh).not.toHaveBeenCalled();
    expect(useAuthStore.getState().accessToken).toBe("stale-access");
  });
});

describe("exchangeOidcHandoff", () => {
  it("posts exactly {} as JSON, with credentials, and returns the tokens and a safe next", async () => {
    const seen: Array<{ body: string; type: string | null; credentials: RequestCredentials }> = [];
    server.use(
      http.post("/api/auth/oidc/exchange", async ({ request }) => {
        seen.push({
          body: await request.text(),
          type: request.headers.get("Content-Type"),
          credentials: request.credentials,
        });
        return HttpResponse.json({
          access_token: "A1",
          refresh_token: "R1",
          token_type: "bearer",
          next: "/reservations",
        });
      }),
    );
    await expect(exchangeOidcHandoff()).resolves.toEqual({
      access_token: "A1",
      refresh_token: "R1",
      next: "/reservations",
    });
    expect(seen).toHaveLength(1);
    expect(seen[0].body).toBe("{}");
    expect(seen[0].type).toMatch(/^application\/json/);
    expect(seen[0].credentials).toBe("include");
  });

  it("stores nothing itself: the caller adopts the tokens", async () => {
    server.use(
      http.post("/api/auth/oidc/exchange", () =>
        HttpResponse.json({ access_token: "A1", refresh_token: "R1", next: "/topology" }),
      ),
    );
    await exchangeOidcHandoff();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });

  const EXCHANGE_FAILURES: Array<[string, () => Response]> = [
    ["the 401", () => HttpResponse.json({ detail: "Single sign-on session expired; sign in again" }, { status: 401 })],
    ["a 404 outside OIDC mode", () => HttpResponse.json({ detail: "Single sign-on is not enabled" }, { status: 404 })],
    ["a 422", () => HttpResponse.json({ detail: [{ msg: "x" }] }, { status: 422 })],
    ["a 500", () => HttpResponse.json({ detail: "boom" }, { status: 500 })],
    ["a network error", () => HttpResponse.error()],
    ["a 200 without tokens", () => HttpResponse.json({ next: "/topology" })],
    ["a 200 that is not JSON", () => new HttpResponse("<html></html>", { status: 200 })],
  ];
  it.each(EXCHANGE_FAILURES)("rejects on %s with no server text in the error", async (_label, answer) => {
    server.use(http.post("/api/auth/oidc/exchange", answer));
    const error = await exchangeOidcHandoff().then(
      () => null,
      (err: unknown) => err,
    );
    expect(error).not.toBeNull();
    const text = String((error as Error).message);
    expect(text).not.toContain("Single sign-on session expired");
    expect(text).not.toContain("boom");
  });

  it("a 401 from it never triggers a token refresh, even with a stored session", async () => {
    useAuthStore.getState().setTokens("stale-access", "some-refresh");
    const refresh = vi.fn();
    let exchanges = 0;
    server.use(
      http.post("/api/auth/oidc/exchange", () => {
        exchanges += 1;
        return HttpResponse.json({ detail: "expired" }, { status: 401 });
      }),
      http.post("/api/auth/refresh", () => {
        refresh();
        return HttpResponse.json({ access_token: "n", refresh_token: "n" });
      }),
    );
    await expect(exchangeOidcHandoff()).rejects.toThrow();
    expect(refresh).not.toHaveBeenCalled();
    expect(exchanges).toBe(1);
    expect(useAuthStore.getState().refreshToken).toBe("some-refresh");
  });
});

describe("adoptSignInTokens: one session path for password and single sign-on", () => {
  it("stores the pair in shared storage, signs the tab in, and refetches the current user", () => {
    const client = newClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    adoptSignInTokens(client, { access_token: "A2", refresh_token: "R2" });
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBe("A2");
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe("R2");
    expect(useAuthStore.getState()).toMatchObject({
      accessToken: "A2",
      refreshToken: "R2",
      isAuthenticated: true,
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["currentUser"] });
  });

  it("is what useLogin runs on success", async () => {
    server.use(
      http.post("/api/auth/login", () => HttpResponse.json({ access_token: "PA", refresh_token: "PR" })),
    );
    const client = newClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useLogin(), { wrapper: makeWrapper(client) });
    result.current.mutate({ email: "u@test", password: "password" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBe("PA");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["currentUser"] });
  });
});
