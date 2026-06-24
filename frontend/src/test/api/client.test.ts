import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { useAuthStore } from "@/stores/authStore";
import apiClient from "@/api/client";

describe("apiClient interceptors", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
    });
  });

  it("clears auth when 401 received with no refresh token", async () => {
    useAuthStore.getState().setTokens("expired-token", "");
    // Clear refresh token but keep access token
    useAuthStore.setState({ refreshToken: null });

    server.use(
      http.get("/api/test-endpoint", () => {
        return HttpResponse.json({ detail: "Unauthorized" }, { status: 401 });
      }),
    );

    await expect(apiClient.get("/test-endpoint")).rejects.toThrow();
    const state = useAuthStore.getState();
    expect(state.accessToken).toBeNull();
    expect(state.isAuthenticated).toBe(false);
  });

  it("refreshes token and retries original request on 401", async () => {
    useAuthStore.getState().setTokens("old-access", "valid-refresh");

    let callCount = 0;
    server.use(
      http.get("/api/test-endpoint", ({ request }) => {
        callCount++;
        const auth = request.headers.get("Authorization");
        if (auth === "Bearer old-access") {
          return HttpResponse.json({ detail: "Unauthorized" }, { status: 401 });
        }
        if (auth === "Bearer new-access") {
          return HttpResponse.json({ data: "success" });
        }
        return HttpResponse.json({ detail: "Unexpected" }, { status: 500 });
      }),
      http.post("/api/auth/refresh", () => {
        return HttpResponse.json({
          access_token: "new-access",
          refresh_token: "new-refresh",
        });
      }),
    );

    const response = await apiClient.get("/test-endpoint");
    expect(response.data).toEqual({ data: "success" });
    expect(callCount).toBe(2);
    expect(useAuthStore.getState().accessToken).toBe("new-access");
    expect(useAuthStore.getState().refreshToken).toBe("new-refresh");
  });

  it("clears auth when refresh request itself fails", async () => {
    useAuthStore.getState().setTokens("old-access", "bad-refresh");

    server.use(
      http.get("/api/test-endpoint", () => {
        return HttpResponse.json({ detail: "Unauthorized" }, { status: 401 });
      }),
      http.post("/api/auth/refresh", () => {
        return HttpResponse.json({ detail: "Invalid token" }, { status: 401 });
      }),
    );

    await expect(apiClient.get("/test-endpoint")).rejects.toThrow();
    const state = useAuthStore.getState();
    expect(state.accessToken).toBeNull();
    expect(state.isAuthenticated).toBe(false);
  });

  it("does not attempt refresh for non-401 errors", async () => {
    useAuthStore.getState().setTokens("good-access", "valid-refresh");

    let refreshCalled = false;
    server.use(
      http.get("/api/test-endpoint", () => {
        return HttpResponse.json({ detail: "Server error" }, { status: 500 });
      }),
      http.post("/api/auth/refresh", () => {
        refreshCalled = true;
        return HttpResponse.json({ access_token: "x", refresh_token: "y" });
      }),
    );

    await expect(apiClient.get("/test-endpoint")).rejects.toThrow();
    // A 500 must pass straight through; auth state and tokens are untouched.
    expect(refreshCalled).toBe(false);
    expect(useAuthStore.getState().accessToken).toBe("good-access");
  });

  it("does not loop refresh when the refresh endpoint itself returns 401", async () => {
    useAuthStore.getState().setTokens("old-access", "bad-refresh");

    let refreshCalls = 0;
    server.use(
      http.get("/api/test-endpoint", () => {
        return HttpResponse.json({ detail: "Unauthorized" }, { status: 401 });
      }),
      http.post("/api/auth/refresh", () => {
        refreshCalls++;
        return HttpResponse.json({ detail: "Invalid token" }, { status: 401 });
      }),
    );

    await expect(apiClient.get("/test-endpoint")).rejects.toThrow();
    // isAuthUrl short-circuits the 401 on /auth/refresh, so it is hit once only.
    expect(refreshCalls).toBe(1);
  });

  it("queues concurrent 401s behind a single refresh and retries all of them", async () => {
    useAuthStore.getState().setTokens("old-access", "valid-refresh");

    let protectedCalls = 0;
    let refreshCalls = 0;
    server.use(
      http.get("/api/endpoint-a", ({ request }) => {
        protectedCalls++;
        const auth = request.headers.get("Authorization");
        if (auth === "Bearer new-access") return HttpResponse.json({ data: "a" });
        return HttpResponse.json({ detail: "Unauthorized" }, { status: 401 });
      }),
      http.get("/api/endpoint-b", ({ request }) => {
        protectedCalls++;
        const auth = request.headers.get("Authorization");
        if (auth === "Bearer new-access") return HttpResponse.json({ data: "b" });
        return HttpResponse.json({ detail: "Unauthorized" }, { status: 401 });
      }),
      http.post("/api/auth/refresh", async () => {
        refreshCalls++;
        // Small delay so the second request enters the isRefreshing queue
        // rather than starting its own refresh.
        await new Promise((r) => setTimeout(r, 20));
        return HttpResponse.json({
          access_token: "new-access",
          refresh_token: "new-refresh",
        });
      }),
    );

    const [resA, resB] = await Promise.all([
      apiClient.get("/endpoint-a"),
      apiClient.get("/endpoint-b"),
    ]);

    expect(resA.data).toEqual({ data: "a" });
    expect(resB.data).toEqual({ data: "b" });
    // Exactly one refresh served both requests; each protected route was hit
    // twice (initial 401 plus the retry with the new token).
    expect(refreshCalls).toBe(1);
    expect(protectedCalls).toBe(4);
    expect(useAuthStore.getState().accessToken).toBe("new-access");
  });

  it("rejects queued requests when the shared refresh fails", async () => {
    useAuthStore.getState().setTokens("old-access", "bad-refresh");

    server.use(
      http.get("/api/endpoint-a", () =>
        HttpResponse.json({ detail: "Unauthorized" }, { status: 401 }),
      ),
      http.get("/api/endpoint-b", () =>
        HttpResponse.json({ detail: "Unauthorized" }, { status: 401 }),
      ),
      http.post("/api/auth/refresh", async () => {
        await new Promise((r) => setTimeout(r, 20));
        return HttpResponse.json({ detail: "Invalid token" }, { status: 401 });
      }),
    );

    // Both the refresher and the queued request must reject, and auth is cleared.
    const results = await Promise.allSettled([
      apiClient.get("/endpoint-a"),
      apiClient.get("/endpoint-b"),
    ]);
    expect(results[0].status).toBe("rejected");
    expect(results[1].status).toBe("rejected");
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });
});
