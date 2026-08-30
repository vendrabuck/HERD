import { http, HttpResponse } from "msw";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useTopologyTemplates,
  useTopologyTemplate,
  useCreateTemplateFromTopology,
  useDeleteTopologyTemplate,
  useInstantiateTemplate,
} from "@/api/topologyTemplates";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const TOPO = "11111111-1111-1111-1111-111111111111";
const TEMPLATE = "22222222-2222-2222-2222-222222222222";
const NEW_TOPO = "33333333-3333-3333-3333-333333333333";

const TEMPLATE_ROW = {
  id: TEMPLATE,
  name: "Spine Leaf",
  description: "two spines two leaves",
  created_by: "user-1",
  owner_name: "Alice",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("topologyTemplates api hooks", () => {
  it("useTopologyTemplates lists with skip/limit query params", async () => {
    let capturedUrl = "";
    server.use(
      http.get("/api/cabling/templates", ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ items: [TEMPLATE_ROW], total: 1, skip: 0, limit: 50 });
      }),
    );
    const { result } = renderHook(() => useTopologyTemplates(0, 50), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toEqual([TEMPLATE_ROW]);
    expect(result.current.data?.total).toBe(1);
    expect(capturedUrl).toContain("skip=0");
    expect(capturedUrl).toContain("limit=50");
  });

  it("useTopologyTemplate is disabled without an id (no request fired)", () => {
    const { result } = renderHook(() => useTopologyTemplate(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useTopologyTemplate fetches by id", async () => {
    server.use(
      http.get(`/api/cabling/templates/${TEMPLATE}`, () =>
        HttpResponse.json({ ...TEMPLATE_ROW, canvas_data: { nodes: [], edges: [] } }),
      ),
    );
    const { result } = renderHook(() => useTopologyTemplate(TEMPLATE), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.id).toBe(TEMPLATE);
  });

  it("creates a template from a topology, with topologyId kept out of the request body", async () => {
    let captured: { name?: string } | null = null;
    let capturedUrl = "";
    server.use(
      http.post(`/api/cabling/templates/from-topology/${TOPO}`, async ({ request }) => {
        capturedUrl = request.url;
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
    let data: { id: string } | undefined;
    await act(async () => {
      data = await result.current.mutateAsync({
        topologyId: TOPO,
        name: "Standard 2-Spine",
      });
    });
    expect(capturedUrl).toMatch(new RegExp(`/templates/from-topology/${TOPO}$`));
    expect(captured).toEqual({ name: "Standard 2-Spine" });
    expect(data?.id).toBe(TEMPLATE);
  });

  it("useDeleteTopologyTemplate DELETEs by id", async () => {
    let capturedUrl = "";
    server.use(
      http.delete(`/api/cabling/templates/${TEMPLATE}`, ({ request }) => {
        capturedUrl = request.url;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { result } = renderHook(() => useDeleteTopologyTemplate(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(TEMPLATE);
    });
    expect(capturedUrl).toMatch(new RegExp(`/templates/${TEMPLATE}$`));
  });

  it("instantiates a template with role assignments", async () => {
    let captured: { name?: string; role_assignments?: Record<string, string> } | null = null;
    let capturedUrl = "";
    server.use(
      http.post(`/api/cabling/templates/${TEMPLATE}/instantiate`, async ({ request }) => {
        capturedUrl = request.url;
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
    let data: { id: string } | undefined;
    await act(async () => {
      data = await result.current.mutateAsync({
        id: TEMPLATE,
        name: "Lab",
        role_assignments: { "pa-vm-1": "device-uuid-1" },
      });
    });
    expect(capturedUrl).toMatch(new RegExp(`/templates/${TEMPLATE}/instantiate$`));
    expect(captured).toEqual({
      name: "Lab",
      role_assignments: { "pa-vm-1": "device-uuid-1" },
    });
    expect(data?.id).toBe(NEW_TOPO);
  });

  it("useInstantiateTemplate surfaces a server error via isError rather than throwing unhandled", async () => {
    server.use(
      http.post(`/api/cabling/templates/${TEMPLATE}/instantiate`, () =>
        HttpResponse.json({ detail: "role spine has no device assigned" }, { status: 422 }),
      ),
    );
    const { result } = renderHook(() => useInstantiateTemplate(), { wrapper });
    await act(async () => {
      await result.current
        .mutateAsync({ id: TEMPLATE, name: "x", role_assignments: {} })
        .catch(() => {});
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
