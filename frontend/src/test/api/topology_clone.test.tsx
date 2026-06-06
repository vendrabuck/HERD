import { http, HttpResponse } from "msw";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { useCloneTopology } from "@/api/topologies";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const SOURCE = "11111111-1111-1111-1111-111111111111";
const CLONE = "22222222-2222-2222-2222-222222222222";

describe("useCloneTopology", () => {
  it("POSTs the new name to the clone endpoint and returns the new topology", async () => {
    let captured: { name?: string } | null = null;
    server.use(
      http.post(`/api/cabling/topologies/${SOURCE}/clone`, async ({ request }) => {
        captured = (await request.json()) as { name?: string };
        return HttpResponse.json({
          id: CLONE,
          name: captured?.name ?? "",
          created_by: "u1",
          owner_name: "viewer",
          created_at: "2026-05-02T00:00:00+00:00",
          updated_at: "2026-05-02T00:00:00+00:00",
          canvas_data: { nodes: [], edges: [] },
        });
      }),
    );

    const { result } = renderHook(() => useCloneTopology(), { wrapper });
    const data = await result.current.mutateAsync({ id: SOURCE, name: "Lab (copy)" });
    expect(captured).toEqual({ name: "Lab (copy)" });
    expect(data.id).toBe(CLONE);
    expect(data.name).toBe("Lab (copy)");
  });
});
