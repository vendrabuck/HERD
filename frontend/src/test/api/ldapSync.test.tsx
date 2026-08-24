import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { server } from "../mocks/server";
import {
  useLdapSyncStatus,
  usePaginatedMappings,
  useCreateMapping,
  useDeleteMapping,
  usePaginatedSyncRuns,
  useStartSyncRun,
} from "@/api/ldapSync";
import type { LdapSyncRun } from "@/types/ldapSync.types";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

// Shared-client wrapper factory for the invalidation tests below: those need
// the paginated query and the mutation hook to see the SAME QueryClient
// (renderHook's default `wrapper` builds a fresh client per call), so the
// mutation's onSuccess invalidation can actually be observed against the
// query it targets.
function makeSharedWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function SharedWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return { client, SharedWrapper };
}

const STATUS = { auth_method: "ldap", group_sync_enabled: true, sync_interval_seconds: 900 };

const MAPPING = {
  id: "m1",
  group_dn: "cn=herd-eng,ou=groups,dc=company,dc=local",
  directory_name: "herd-eng",
  herd_group_id: "g1",
  created_by: null,
  created_at: "2026-01-01T00:00:00Z",
};

function makeRun(overrides: Partial<LdapSyncRun> = {}): LdapSyncRun {
  return {
    id: "r1",
    started_at: new Date().toISOString(),
    finished_at: null,
    trigger: "manual",
    status: "running",
    users_provisioned: 0,
    members_added: 0,
    members_removed: 0,
    members_skipped: 0,
    users_deactivated: 0,
    users_reactivated: 0,
    detail: {},
    error: null,
    ...overrides,
  };
}

describe("ldapSync api fetchers: path, verb, and params", () => {
  it("useLdapSyncStatus GETs /auth/admin/ldap-sync/status", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/auth/admin/ldap-sync/status", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json(STATUS);
      }),
    );
    const { result } = renderHook(() => useLdapSyncStatus(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(STATUS);
    expect(capturedUrl).toMatch(/\/auth\/admin\/ldap-sync\/status$/);
  });

  it("usePaginatedMappings GETs /auth/admin/ldap-sync/mappings with skip/limit params", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/auth/admin/ldap-sync/mappings", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ items: [MAPPING], total: 1, skip: 25, limit: 10 });
      }),
    );
    const { result } = renderHook(() => usePaginatedMappings(25, 10), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toEqual([MAPPING]);
    const parsed = new URL(capturedUrl);
    expect(parsed.pathname).toMatch(/\/auth\/admin\/ldap-sync\/mappings$/);
    expect(parsed.searchParams.get("skip")).toBe("25");
    expect(parsed.searchParams.get("limit")).toBe("10");
  });

  it("usePaginatedMappings defaults skip=0 limit=50 when called with no args", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/auth/admin/ldap-sync/mappings", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 });
      }),
    );
    const { result } = renderHook(() => usePaginatedMappings(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const parsed = new URL(capturedUrl);
    expect(parsed.searchParams.get("skip")).toBe("0");
    expect(parsed.searchParams.get("limit")).toBe("50");
  });

  it("useCreateMapping POSTs the body to /auth/admin/ldap-sync/mappings", async () => {
    let captured: unknown = null;
    let capturedUrl = "";
    server.use(
      http.post("/api/auth/admin/ldap-sync/mappings", async ({ request }) => {
        capturedUrl = request.url;
        captured = await request.json();
        return HttpResponse.json({ ...MAPPING, warning: null });
      }),
    );
    const { result } = renderHook(() => useCreateMapping(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ group_dn: "cn=herd-eng,ou=groups,dc=company,dc=local", herd_group_id: "g1" });
    });
    expect(capturedUrl).toMatch(/\/auth\/admin\/ldap-sync\/mappings$/);
    expect(captured).toEqual({
      group_dn: "cn=herd-eng,ou=groups,dc=company,dc=local",
      herd_group_id: "g1",
    });
  });

  it("useDeleteMapping DELETEs /auth/admin/ldap-sync/mappings/{id}", async () => {
    let capturedUrl = "";
    server.use(
      http.delete("/api/auth/admin/ldap-sync/mappings/m1", ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteMapping(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("m1");
    });
    expect(capturedUrl).toMatch(/\/auth\/admin\/ldap-sync\/mappings\/m1$/);
  });

  it("usePaginatedSyncRuns GETs /auth/admin/ldap-sync/runs with skip/limit params", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/auth/admin/ldap-sync/runs", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({
          items: [makeRun({ status: "success" })],
          total: 1,
          skip: 10,
          limit: 5,
        });
      }),
    );
    const { result } = renderHook(() => usePaginatedSyncRuns(10, 5), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const parsed = new URL(capturedUrl);
    expect(parsed.pathname).toMatch(/\/auth\/admin\/ldap-sync\/runs$/);
    expect(parsed.searchParams.get("skip")).toBe("10");
    expect(parsed.searchParams.get("limit")).toBe("5");
  });

  it("useStartSyncRun POSTs /auth/admin/ldap-sync/run with no body", async () => {
    let capturedUrl = "";
    let hadBody = false;
    server.use(
      http.post("/api/auth/admin/ldap-sync/run", async ({ request }) => {
        capturedUrl = request.url;
        const text = await request.text();
        hadBody = text.length > 0;
        return HttpResponse.json({ run_id: "r1" });
      }),
    );
    const { result } = renderHook(() => useStartSyncRun(), { wrapper });
    let data: { run_id: string } | undefined;
    await act(async () => {
      data = await result.current.mutateAsync();
    });
    expect(capturedUrl).toMatch(/\/auth\/admin\/ldap-sync\/run$/);
    expect(hadBody).toBe(false);
    expect(data?.run_id).toBe("r1");
  });
});

describe("ldapSync mutation cache invalidation", () => {
  it("useCreateMapping invalidates the ['ldap-sync', 'mappings', ...] query and it refetches", async () => {
    let getCalls = 0;
    server.use(
      http.get("/api/auth/admin/ldap-sync/mappings", () => {
        getCalls += 1;
        return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 25 });
      }),
      http.post("/api/auth/admin/ldap-sync/mappings", () =>
        HttpResponse.json({ ...MAPPING, warning: null }),
      ),
    );
    const { client, SharedWrapper } = makeSharedWrapper();

    const mappingsHook = renderHook(() => usePaginatedMappings(0, 25), {
      wrapper: SharedWrapper,
    });
    await waitFor(() => expect(mappingsHook.result.current.isSuccess).toBe(true));
    expect(getCalls).toBe(1);

    const mutationHook = renderHook(() => useCreateMapping(), { wrapper: SharedWrapper });
    await act(async () => {
      await mutationHook.result.current.mutateAsync({
        group_dn: "cn=herd-eng,ou=groups,dc=company,dc=local",
        herd_group_id: "g1",
      });
    });

    // The seeded query under the prefixed key was invalidated and refetched:
    // a second GET landed, and the query's own invalidation flag cleared
    // once that refetch completed.
    await waitFor(() => expect(getCalls).toBe(2));
    await waitFor(() =>
      expect(client.getQueryState(["ldap-sync", "mappings", 0, 25])?.isInvalidated).toBe(false),
    );
  });

  it("useDeleteMapping invalidates the ['ldap-sync', 'mappings', ...] query and it refetches", async () => {
    let getCalls = 0;
    server.use(
      http.get("/api/auth/admin/ldap-sync/mappings", () => {
        getCalls += 1;
        return HttpResponse.json({ items: [MAPPING], total: 1, skip: 0, limit: 25 });
      }),
      http.delete("/api/auth/admin/ldap-sync/mappings/m1", () => new HttpResponse(null, { status: 204 })),
    );
    const { client, SharedWrapper } = makeSharedWrapper();

    const mappingsHook = renderHook(() => usePaginatedMappings(0, 25), {
      wrapper: SharedWrapper,
    });
    await waitFor(() => expect(mappingsHook.result.current.isSuccess).toBe(true));
    expect(getCalls).toBe(1);

    const mutationHook = renderHook(() => useDeleteMapping(), { wrapper: SharedWrapper });
    await act(async () => {
      await mutationHook.result.current.mutateAsync("m1");
    });

    await waitFor(() => expect(getCalls).toBe(2));
    await waitFor(() =>
      expect(client.getQueryState(["ldap-sync", "mappings", 0, 25])?.isInvalidated).toBe(false),
    );
  });

  it("useStartSyncRun invalidates the ['ldap-sync', 'runs', ...] query and it refetches", async () => {
    let getCalls = 0;
    server.use(
      http.get("/api/auth/admin/ldap-sync/runs", () => {
        getCalls += 1;
        return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 25 });
      }),
      http.post("/api/auth/admin/ldap-sync/run", () => HttpResponse.json({ run_id: "r1" })),
    );
    const { client, SharedWrapper } = makeSharedWrapper();

    const runsHook = renderHook(() => usePaginatedSyncRuns(0, 25), { wrapper: SharedWrapper });
    await waitFor(() => expect(runsHook.result.current.isSuccess).toBe(true));
    expect(getCalls).toBe(1);

    const mutationHook = renderHook(() => useStartSyncRun(), { wrapper: SharedWrapper });
    await act(async () => {
      await mutationHook.result.current.mutateAsync();
    });

    await waitFor(() => expect(getCalls).toBe(2));
    await waitFor(() =>
      expect(client.getQueryState(["ldap-sync", "runs", 0, 25])?.isInvalidated).toBe(false),
    );
  });
});

describe("useLdapSyncStatus has no placeholderData (load-bearing for the fail-closed actionsEnabled gate)", () => {
  it("data is undefined (not a stale/placeholder value) while the status query is loading", async () => {
    server.use(http.get("/api/auth/admin/ldap-sync/status", () => HttpResponse.json(STATUS)));
    const { result } = renderHook(() => useLdapSyncStatus(), { wrapper });

    // Assert BEFORE the fetch resolves: with no placeholderData, TanStack
    // Query has nothing to show yet, so `data` is undefined and the page's
    // `status?.auth_method === "ldap"` gate is correctly false. If this hook
    // ever gained a literal placeholderData (or keepPreviousData, which would
    // matter as soon as any param varied the key), this first read would
    // come back populated instead, silently defeating the fail-closed gate
    // the module comment calls out.
    expect(result.current.isLoading).toBe(true);
    expect(result.current.data).toBeUndefined();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(STATUS);
  });
});

describe("usePaginatedSyncRuns wires runsRefetchInterval to the query", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  // These assert the OBSERVABLE polling effect end-to-end (a real refetch
  // firing on the interval) rather than inspecting query options directly:
  // runsRefetchInterval is already unit-tested in ldapSync.test.ts, so what
  // is unverified per the issue is the WIRING (that usePaginatedSyncRuns's
  // refetchInterval callback actually reads query.state.data and feeds it
  // through). A live refetch count under fake timers pins that wiring
  // without coupling to react-query's internal option shape, which is the
  // least brittle choice available here.
  it("refetches on the 2s interval while a run is actively running", async () => {
    let calls = 0;
    server.use(
      http.get("/api/auth/admin/ldap-sync/runs", () => {
        calls += 1;
        return HttpResponse.json({ items: [makeRun({ status: "running" })], total: 1, skip: 0, limit: 50 });
      }),
    );
    const { result } = renderHook(() => usePaginatedSyncRuns(0, 50), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(calls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    await waitFor(() => expect(calls).toBe(2));
  });

  it("does not poll again once every run on the page is terminal", async () => {
    let calls = 0;
    server.use(
      http.get("/api/auth/admin/ldap-sync/runs", () => {
        calls += 1;
        return HttpResponse.json({
          items: [makeRun({ status: "success" }), makeRun({ id: "r2", status: "failed" })],
          total: 2,
          skip: 0,
          limit: 50,
        });
      }),
    );
    const { result } = renderHook(() => usePaginatedSyncRuns(0, 50), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(calls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(calls).toBe(1);
  });
});
