import { http, HttpResponse } from "msw";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useCreateTemplateFromTopology,
  useInstantiateTemplate,
} from "@/api/topologyTemplates";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const TOPO = "11111111-1111-1111-1111-111111111111";
const TEMPLATE = "22222222-2222-2222-2222-222222222222";
const NEW_TOPO = "33333333-3333-3333-3333-333333333333";

describe("topology template api hooks", () => {
  it("creates a template from a topology", async () => {
    let captured: { name?: string } | null = null;
    server.use(
      http.post(`/api/cabling/templates/from-topology/${TOPO}`, async ({ request }) => {
        captured = (await request.json()) as { name?: string };
        return HttpResponse.json({
          id: TEMPLATE,
          name: captured?.name ?? "",
          description: null,
          canvas_data: { nodes: [], edges: [] },
          created_by: "u1",
          owner_name: "viewer",
          created_at: "2026-05-02T00:00:00+00:00",
          updated_at: "2026-05-02T00:00:00+00:00",
        });
      }),
    );

    const { result } = renderHook(() => useCreateTemplateFromTopology(), { wrapper });
    const data = await result.current.mutateAsync({
      topologyId: TOPO,
      name: "Standard 2-Spine",
    });
    expect(captured).toEqual({ name: "Standard 2-Spine" });
    expect(data.id).toBe(TEMPLATE);
  });

  it("instantiates a template with role assignments", async () => {
    let captured: { name?: string; role_assignments?: Record<string, string> } | null = null;
    server.use(
      http.post(`/api/cabling/templates/${TEMPLATE}/instantiate`, async ({ request }) => {
        captured = (await request.json()) as {
          name?: string;
          role_assignments?: Record<string, string>;
        };
        return HttpResponse.json({
          id: NEW_TOPO,
          name: captured?.name ?? "",
          created_by: "u1",
          owner_name: "viewer",
          created_at: "2026-05-02T00:00:00+00:00",
          updated_at: "2026-05-02T00:00:00+00:00",
          canvas_data: { nodes: [], edges: [] },
        });
      }),
    );

    const { result } = renderHook(() => useInstantiateTemplate(), { wrapper });
    const data = await result.current.mutateAsync({
      id: TEMPLATE,
      name: "Lab",
      role_assignments: { "pa-vm-1": "device-uuid-1" },
    });
    expect(captured).toEqual({
      name: "Lab",
      role_assignments: { "pa-vm-1": "device-uuid-1" },
    });
    expect(data.id).toBe(NEW_TOPO);
  });
});
