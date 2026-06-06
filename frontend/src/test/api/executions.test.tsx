import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useCommandLog } from "@/api/executions";
import type { CommandLogEntry } from "@/types/ai.types";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function makeEntry(overrides: Partial<CommandLogEntry> = {}): CommandLogEntry {
  return {
    id: "cmd-1",
    run_id: "run-1",
    seq: 0,
    command: "show version",
    response: "Cisco IOS Software, Version 15.2",
    duration_ms: 42,
    exit_status: "ok",
    created_at: "2026-05-31T00:00:00Z",
    ...overrides,
  };
}

describe("useCommandLog", () => {
  it("fetches the command log for a run and returns the populated list", async () => {
    const entries = [
      makeEntry({ id: "cmd-1", seq: 0, command: "show version" }),
      makeEntry({ id: "cmd-2", seq: 1, command: "show interfaces" }),
    ];
    let requestedUrl: string | undefined;
    server.use(
      http.get("/api/execution/runs/run-1/commands", ({ request }) => {
        requestedUrl = request.url;
        return HttpResponse.json(entries);
      }),
    );

    const { result } = renderHook(() => useCommandLog("run-1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(requestedUrl).toContain("/api/execution/runs/run-1/commands");
    expect(result.current.data).toHaveLength(2);
    expect(result.current.data?.[1]).toMatchObject({
      id: "cmd-2",
      command: "show interfaces",
    });
  });

  it("returns an empty array when the run has no commands", async () => {
    server.use(
      http.get("/api/execution/runs/run-empty/commands", () =>
        HttpResponse.json([]),
      ),
    );

    const { result } = renderHook(() => useCommandLog("run-empty"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toEqual([]);
  });

  it("surfaces an error state when the server returns a failure", async () => {
    server.use(
      http.get("/api/execution/runs/run-err/commands", () =>
        HttpResponse.json({ detail: "not found" }, { status: 404 }),
      ),
    );

    const { result } = renderHook(() => useCommandLog("run-err"), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.data).toBeUndefined();
  });

  it("does not run the query when runId is null", async () => {
    let called = false;
    server.use(
      http.get("/api/execution/runs/:runId/commands", () => {
        called = true;
        return HttpResponse.json([]);
      }),
    );

    const { result } = renderHook(() => useCommandLog(null), { wrapper });

    expect(result.current.isLoading).toBe(false);
    expect(result.current.fetchStatus).toBe("idle");
    expect(called).toBe(false);
  });

  it("does not run the query when enabled is false even with a runId", async () => {
    let called = false;
    server.use(
      http.get("/api/execution/runs/:runId/commands", () => {
        called = true;
        return HttpResponse.json([]);
      }),
    );

    const { result } = renderHook(() => useCommandLog("run-1", false), {
      wrapper,
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(called).toBe(false);
  });
});
