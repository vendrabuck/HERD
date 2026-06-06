import { http, HttpResponse } from "msw";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useScheduleApplyJob,
  useCancelApplyJob,
} from "@/api/deviceConfigJobs";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const DEVICE = "11111111-1111-1111-1111-111111111111";
const VERSION = "22222222-2222-2222-2222-222222222222";
const JOB = "33333333-3333-3333-3333-333333333333";

describe("device config job api hooks", () => {
  it("schedule posts the body to the version endpoint", async () => {
    let captured: { scheduled_for?: string; reservation_id?: string } | null = null;
    server.use(
      http.post(
        `/api/inventory/devices/${DEVICE}/config-versions/${VERSION}/schedule`,
        async ({ request }) => {
          captured = (await request.json()) as {
            scheduled_for?: string;
            reservation_id?: string;
          };
          return HttpResponse.json({
            id: JOB,
            device_id: DEVICE,
            version_id: VERSION,
            scheduled_for: captured?.scheduled_for ?? "",
            reservation_id: null,
            status: "pending",
            run_id: null,
            error: null,
            created_by: "u1",
            author_name: "alice",
            created_at: "2026-05-02T00:00:00+00:00",
            fired_at: null,
          });
        },
      ),
    );

    const { result } = renderHook(() => useScheduleApplyJob(DEVICE), { wrapper });
    const data = await result.current.mutateAsync({
      versionId: VERSION,
      scheduled_for: "2026-05-02T12:00:00+00:00",
    });
    expect(captured).toEqual({ scheduled_for: "2026-05-02T12:00:00+00:00" });
    expect(data.id).toBe(JOB);
    expect(data.status).toBe("pending");
  });

  it("cancel hits DELETE on the job id", async () => {
    let called = false;
    server.use(
      http.delete(`/api/inventory/apply-jobs/${JOB}`, () => {
        called = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { result } = renderHook(() => useCancelApplyJob(DEVICE), { wrapper });
    await result.current.mutateAsync(JOB);
    expect(called).toBe(true);
  });
});
