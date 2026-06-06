import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useGrants, useCreateGrant, useDeleteGrant } from "@/api/acl";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const GRANT = {
  id: "g1",
  group_id: "ug-1",
  resource_type: "device",
  resource_id: "d-1",
  permission: "use",
};

describe("acl api hooks", () => {
  it("useGrants fetches with no filters", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/acl/grants", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json([GRANT]);
      }),
    );
    const { result } = renderHook(() => useGrants(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toHaveLength(1);
    expect(capturedUrl).not.toMatch(/\?/);
  });

  it("useGrants forwards filter params", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/acl/grants", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json([]);
      }),
    );
    const { result } = renderHook(
      () =>
        useGrants({
          group_id: "ug-1",
          resource_type: "device",
          resource_id: "d-1",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(capturedUrl).toMatch(/group_id=ug-1/);
    expect(capturedUrl).toMatch(/resource_type=device/);
    expect(capturedUrl).toMatch(/resource_id=d-1/);
  });

  it("useCreateGrant posts to /acl/grants", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/acl/grants", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(GRANT);
      }),
    );
    const { result } = renderHook(() => useCreateGrant(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        group_id: "ug-1",
        resource_type: "device",
        resource_id: "d-1",
        permission: "view",
      });
    });
    expect((captured as { group_id: string }).group_id).toBe("ug-1");
  });

  it("useDeleteGrant deletes by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/acl/grants/g1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteGrant(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("g1");
    });
    expect(capturedUrl).toMatch(/\/api\/acl\/grants\/g1$/);
  });
});
