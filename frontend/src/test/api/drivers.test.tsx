import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useDriver,
  useDrivers,
  usePaginatedDrivers,
  useCreateDriver,
  useUpdateDriver,
  useDeleteDriver,
  useDownloadDriver,
} from "@/api/drivers";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const DRV = {
  id: "d1",
  name: "Junos",
  description: "fw",
  connection_type: "Management",
  filename: "junos.zip",
  size_bytes: 1024,
  sha256: "abc",
  uploaded_by: "admin",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("drivers api hooks", () => {
  it("useDriver is disabled without an id", () => {
    const { result } = renderHook(() => useDriver(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useDrivers returns items", async () => {
    server.use(
      http.get("/api/inventory/drivers", () =>
        HttpResponse.json({ items: [DRV], total: 1, skip: 0, limit: 500 }),
      ),
    );
    const { result } = renderHook(() => useDrivers(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([DRV]);
  });

  it("usePaginatedDrivers returns total", async () => {
    server.use(
      http.get("/api/inventory/drivers", () =>
        HttpResponse.json({ items: [DRV], total: 3, skip: 0, limit: 50 }),
      ),
    );
    const { result } = renderHook(() => usePaginatedDrivers(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBe(3);
  });

  it("useCreateDriver POSTs multipart with the file", async () => {
    let contentType = "";
    server.use(
      http.post("/api/inventory/drivers", ({ request }) => {
        contentType = request.headers.get("content-type") ?? "";
        return HttpResponse.json(DRV);
      }),
    );
    const { result } = renderHook(() => useCreateDriver(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        name: "Junos",
        connection_type: "Management",
        file: new File(["x"], "j.zip", { type: "application/zip" }),
      });
    });
    expect(contentType).toMatch(/multipart\/form-data/);
  });

  it("useUpdateDriver PUTs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.put("/api/inventory/drivers/d1", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json(DRV);
      }),
    );
    const { result } = renderHook(() => useUpdateDriver(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        id: "d1",
        data: { name: "New", description: undefined, connection_type: "Management" },
      });
    });
    expect(capturedUrl).toMatch(/\/drivers\/d1$/);
  });

  it("useDeleteDriver DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/inventory/drivers/d1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteDriver(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("d1");
    });
    expect(capturedUrl).toMatch(/d1$/);
  });

  it("useDownloadDriver exposes a mutate function", () => {
    // The blob-response path is exercised end-to-end in tests/e2e and is hard
    // to mock under msw + jsdom because Response stream extraction relies on
    // a streamable body. Smoke-check that the hook is callable.
    const { result } = renderHook(() => useDownloadDriver(), { wrapper });
    expect(typeof result.current.mutate).toBe("function");
    expect(result.current.isPending).toBe(false);
  });
});
