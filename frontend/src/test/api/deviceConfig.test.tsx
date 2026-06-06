import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useDeviceConfigVersions,
  useCreateDeviceConfigVersion,
  useDeviceConfigDiff,
  useApplyDeviceConfigVersion,
} from "@/api/deviceConfig";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const DEVICE = "11111111-1111-1111-1111-111111111111";
const VA = "22222222-2222-2222-2222-222222222222";
const VB = "33333333-3333-3333-3333-333333333333";

describe("device config api hooks", () => {
  it("lists config versions", async () => {
    server.use(
      http.get(`/api/inventory/devices/${DEVICE}/config-versions`, () =>
        HttpResponse.json({
          items: [
            {
              id: VA,
              device_id: DEVICE,
              version_number: 2,
              connection_type: "Management",
              description: "second",
              created_by: "u1",
              author_name: "alice",
              created_at: "2026-05-02T00:00:00+00:00",
              restored_from_id: null,
              last_apply_run_id: null,
            },
          ],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );

    const { result } = renderHook(() => useDeviceConfigVersions(DEVICE), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items[0].version_number).toBe(2);
  });

  it("posts a new version body", async () => {
    let captured: { config?: unknown; description?: string } | null = null;
    server.use(
      http.post(`/api/inventory/devices/${DEVICE}/config-versions`, async ({ request }) => {
        captured = (await request.json()) as { config?: unknown; description?: string };
        return HttpResponse.json({
          id: VA,
          device_id: DEVICE,
          version_number: 1,
          connection_type: "Management",
          config: { vlan: 100 },
          description: "first",
          created_by: "u1",
          author_name: "alice",
          created_at: "2026-05-02T00:00:00+00:00",
          restored_from_id: null,
          last_apply_run_id: null,
        });
      }),
    );

    const { result } = renderHook(() => useCreateDeviceConfigVersion(DEVICE), { wrapper });
    await result.current.mutateAsync({ config: { vlan: 100 }, description: "first" });
    expect(captured).toEqual({ config: { vlan: 100 }, description: "first" });
  });

  it("fetches the diff with from/to query params", async () => {
    const captured: { url?: URL } = {};
    server.use(
      http.get(`/api/inventory/devices/${DEVICE}/config-versions/diff`, ({ request }) => {
        captured.url = new URL(request.url);
        return HttpResponse.json({
          version_a: VA,
          version_b: VB,
          diff: "@@ -1 +1 @@\n-100\n+200",
        });
      }),
    );

    const { result } = renderHook(() => useDeviceConfigDiff(DEVICE, VA, VB), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(captured.url?.searchParams.get("from")).toBe(VA);
    expect(captured.url?.searchParams.get("to")).toBe(VB);
    expect(result.current.data?.diff).toContain("@@");
  });

  it("apply mutation hits the apply endpoint and returns the run id", async () => {
    server.use(
      http.post(
        `/api/inventory/devices/${DEVICE}/config-versions/${VA}/apply`,
        () =>
          HttpResponse.json({
            version_id: VA,
            run_id: VB,
            status: "success",
            error: null,
          }),
      ),
    );

    const { result } = renderHook(() => useApplyDeviceConfigVersion(DEVICE), { wrapper });
    const data = await result.current.mutateAsync(VA);
    expect(data.run_id).toBe(VB);
    expect(data.status).toBe("success");
  });
});
