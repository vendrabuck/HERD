import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, beforeEach } from "vitest";

import { server } from "../mocks/server";
import {
  useConfigStatus,
  useConfigSchema,
  useConfigLogin,
  useApplyConfig,
} from "@/api/config";
import { useConfigStore } from "@/stores/configStore";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("config api hooks", () => {
  beforeEach(() => {
    useConfigStore.setState({ configToken: null });
  });

  it("useConfigStatus reports configured/password_changed flags", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true, password_changed: false }),
      ),
    );
    const { result } = renderHook(() => useConfigStatus(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.configured).toBe(true);
    expect(result.current.data?.password_changed).toBe(false);
  });

  it("useConfigSchema unwraps the fields array", async () => {
    server.use(
      http.get("/api/config/schema", () =>
        HttpResponse.json({
          fields: [
            {
              key: "AUTH_SECRET_KEY",
              label: "Auth secret",
              type: "password",
              required: true,
              group: "auth",
              secret: true,
              description: "",
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useConfigSchema(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.[0].key).toBe("AUTH_SECRET_KEY");
  });

  it("useConfigLogin persists the token on success", async () => {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ token: "cfg-tok", password_changed: false }),
      ),
    );
    const { result } = renderHook(() => useConfigLogin(), { wrapper });
    result.current.mutate("password");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(useConfigStore.getState().configToken).toBe("cfg-tok");
  });

  it("useConfigLogin does not persist a token on failure", async () => {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ detail: "bad password" }, { status: 401 }),
      ),
    );
    const { result } = renderHook(() => useConfigLogin(), { wrapper });
    result.current.mutate("wrong");
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(useConfigStore.getState().configToken).toBeNull();
  });

  it("useApplyConfig forwards the bearer token from the store", async () => {
    useConfigStore.setState({ configToken: "cfg-tok" });
    let auth = "";
    server.use(
      http.post("/api/config/apply", ({ request }) => {
        auth = request.headers.get("authorization") ?? "";
        return HttpResponse.json({ restarted: ["auth"], errors: [] });
      }),
    );
    const { result } = renderHook(() => useApplyConfig(), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(auth).toBe("Bearer cfg-tok");
    expect(result.current.data?.restarted).toEqual(["auth"]);
  });
});
