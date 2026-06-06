import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useGroup,
  useGroups,
  useCreateGroup,
  useUpdateGroup,
  useDeleteGroup,
  useAddMember,
  useRemoveMember,
  useBulkAddMembers,
} from "@/api/groups";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const GROUP = { id: "g1", name: "engineers", description: null };

describe("groups api hooks", () => {
  it("useGroup is disabled without an id", () => {
    const { result } = renderHook(() => useGroup(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useGroups returns items", async () => {
    server.use(
      http.get("/api/auth/groups", () =>
        HttpResponse.json({ items: [GROUP], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useGroups(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([GROUP]);
  });

  it("useCreateGroup POSTs the body", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/auth/groups", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(GROUP);
      }),
    );
    const { result } = renderHook(() => useCreateGroup(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ name: "engineers" });
    });
    expect((captured as { name: string }).name).toBe("engineers");
  });

  it("useUpdateGroup PUTs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.put("/api/auth/groups/g1", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json(GROUP);
      }),
    );
    const { result } = renderHook(() => useUpdateGroup(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ id: "g1", data: { name: "x" } });
    });
    expect(capturedUrl).toMatch(/\/auth\/groups\/g1$/);
  });

  it("useDeleteGroup DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/auth/groups/g1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteGroup(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("g1");
    });
    expect(capturedUrl).toMatch(/\/auth\/groups\/g1$/);
  });

  it("useAddMember POSTs user_id to members", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/auth/groups/g1/members", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({ user_id: "u1", group_id: "g1" });
      }),
    );
    const { result } = renderHook(() => useAddMember(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ groupId: "g1", userId: "u1" });
    });
    expect((captured as { user_id: string }).user_id).toBe("u1");
  });

  it("useRemoveMember DELETEs by member id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/auth/groups/g1/members/u1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useRemoveMember(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ groupId: "g1", userId: "u1" });
    });
    expect(capturedUrl).toMatch(/\/members\/u1$/);
  });

  it("useBulkAddMembers POSTs a user_ids array", async () => {
    let captured: unknown = null;
    server.use(
      http.post(
        "/api/auth/groups/g1/members/bulk",
        async ({ request }) => {
          captured = await request.json();
          return HttpResponse.json({ added: 2, skipped: 0 });
        },
      ),
    );
    const { result } = renderHook(() => useBulkAddMembers(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        groupId: "g1",
        userIds: ["u1", "u2"],
      });
    });
    expect((captured as { user_ids: string[] }).user_ids).toEqual(["u1", "u2"]);
  });
});
