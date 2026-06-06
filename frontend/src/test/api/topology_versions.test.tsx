import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useTopologyVersions,
  useVersionDiff,
  useRestoreVersion,
} from "@/api/topologies";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const TOPO = "11111111-1111-1111-1111-111111111111";
const VA = "22222222-2222-2222-2222-222222222222";
const VB = "33333333-3333-3333-3333-333333333333";

describe("topology version api hooks", () => {
  it("useTopologyVersions returns the paginated list", async () => {
    const payload = {
      items: [
        {
          id: VA,
          topology_id: TOPO,
          version_number: 2,
          name: "Lab",
          description: "second",
          created_by: "u1",
          author_name: "alice",
          created_at: "2026-04-20T00:00:00+00:00",
          restored_from_id: null,
        },
        {
          id: VB,
          topology_id: TOPO,
          version_number: 1,
          name: "Lab",
          description: null,
          created_by: "u1",
          author_name: "alice",
          created_at: "2026-04-19T00:00:00+00:00",
          restored_from_id: null,
        },
      ],
      total: 2,
      skip: 0,
      limit: 50,
    };
    server.use(
      http.get(`/api/cabling/topologies/${TOPO}/versions`, () =>
        HttpResponse.json(payload),
      ),
    );

    const { result } = renderHook(() => useTopologyVersions(TOPO), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toHaveLength(2);
    expect(result.current.data?.items[0].version_number).toBe(2);
  });

  it("useVersionDiff is disabled when a === b", () => {
    const { result } = renderHook(() => useVersionDiff(TOPO, VA, VA), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useVersionDiff fetches when a !== b", async () => {
    const diff = {
      version_a: VA,
      version_b: VB,
      nodes_added: [{ id: "n1" }],
      nodes_removed: [],
      nodes_modified: [],
      edges_added: [],
      edges_removed: [],
      edges_modified: [],
    };
    server.use(
      http.get(`/api/cabling/topologies/${TOPO}/versions/diff`, () =>
        HttpResponse.json(diff),
      ),
    );

    const { result } = renderHook(() => useVersionDiff(TOPO, VA, VB), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.nodes_added).toHaveLength(1);
  });

  it("useRestoreVersion POSTs the request body", async () => {
    const restored = {
      id: TOPO,
      name: "Lab",
      created_by: "u1",
      owner_name: "alice",
      created_at: "2026-04-20T00:00:00+00:00",
      updated_at: "2026-04-20T00:01:00+00:00",
      canvas_data: { nodes: [], edges: [] },
    };
    let captured: { description?: string; restore_name?: boolean } | null = null;
    server.use(
      http.post(
        `/api/cabling/topologies/${TOPO}/versions/${VA}/restore`,
        async ({ request }) => {
          captured = (await request.json()) as {
            description?: string;
            restore_name?: boolean;
          };
          return HttpResponse.json(restored);
        },
      ),
    );

    const { result } = renderHook(() => useRestoreVersion(TOPO), { wrapper });
    await result.current.mutateAsync({
      versionId: VA,
      body: { description: "rollback", restore_name: true },
    });
    expect(captured).toEqual({ description: "rollback", restore_name: true });
  });
});
