import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useAllUsers, usePaginatedUsers, useSetUserRole } from "@/api/admin";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const USER = {
  id: "u1",
  email: "u@test",
  username: "u",
  role: "user",
};

describe("admin api hooks", () => {
  it("useAllUsers returns the items array", async () => {
    server.use(
      http.get("/api/auth/users", () =>
        HttpResponse.json({ items: [USER], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useAllUsers(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([USER]);
  });

  it("useAllUsers respects enabled=false", () => {
    const { result } = renderHook(() => useAllUsers(false), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("usePaginatedUsers returns the paginated envelope", async () => {
    server.use(
      http.get("/api/auth/users", () =>
        HttpResponse.json({ items: [USER], total: 5, skip: 0, limit: 50 }),
      ),
    );
    const { result } = renderHook(() => usePaginatedUsers(0, 50), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBe(5);
  });

  it("useSetUserRole PUTs the new role", async () => {
    let captured: unknown = null;
    server.use(
      http.put("/api/auth/users/u1/role", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({ ...USER, role: "admin" });
      }),
    );
    const { result } = renderHook(() => useSetUserRole(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ userId: "u1", role: "admin" });
    });
    expect((captured as { role: string }).role).toBe("admin");
  });
});
