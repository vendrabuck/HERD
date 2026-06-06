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
});
