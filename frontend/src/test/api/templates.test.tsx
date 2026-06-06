import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useTemplate,
  useTemplates,
  usePaginatedTemplates,
  useCreateTemplate,
  useUpdateTemplate,
  useDeleteTemplate,
} from "@/api/templates";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const TPL = {
  id: "t1",
  name: "Alpha",
  template_type: "PHYSICAL",
  fields: [],
};

describe("templates api hooks", () => {
  it("useTemplate is disabled without an id", () => {
    const { result } = renderHook(() => useTemplate(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useTemplates fetches the items array", async () => {
    server.use(
      http.get("/api/inventory/templates", () =>
        HttpResponse.json({ items: [TPL], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useTemplates(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([TPL]);
  });

  it("useTemplates passes template_type as a filter", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/inventory/templates", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 });
      }),
    );
    const { result } = renderHook(() => useTemplates("PHYSICAL"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(capturedUrl).toMatch(/template_type=PHYSICAL/);
  });

  it("usePaginatedTemplates returns total + items", async () => {
    server.use(
      http.get("/api/inventory/templates", () =>
        HttpResponse.json({ items: [TPL], total: 9, skip: 0, limit: 50 }),
      ),
    );
    const { result } = renderHook(() => usePaginatedTemplates(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBe(9);
  });

  it("useCreateTemplate POSTs", async () => {
    let captured: unknown = null;
    server.use(
      http.post("/api/inventory/templates", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(TPL);
      }),
    );
    const { result } = renderHook(() => useCreateTemplate(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        name: "Alpha",
        template_type: "device",
        sections: [],
      });
    });
    expect((captured as { name: string }).name).toBe("Alpha");
  });

  it("useUpdateTemplate PUTs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.put("/api/inventory/templates/t1", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json(TPL);
      }),
    );
    const { result } = renderHook(() => useUpdateTemplate(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        id: "t1",
        data: { name: "Beta" },
      });
    });
    expect(capturedUrl).toMatch(/\/templates\/t1$/);
  });

  it("useDeleteTemplate DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/inventory/templates/t1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteTemplate(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("t1");
    });
    expect(capturedUrl).toMatch(/\/templates\/t1$/);
  });
});
