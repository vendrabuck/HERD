import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { Mock } from "vitest";

// react-hot-toast's default export is called both as a function (toast(msg)) and
// via its methods (toast.success / toast.error). The mock must satisfy both.
const { toastFn, toastSuccess, toastError } = vi.hoisted(() => {
  const fn = vi.fn() as Mock & { success: Mock; error: Mock };
  const success = vi.fn();
  const error = vi.fn();
  fn.success = success;
  fn.error = error;
  return { toastFn: fn, toastSuccess: success, toastError: error };
});
vi.mock("react-hot-toast", () => ({ default: toastFn }));

import { server } from "../mocks/server";
import {
  useReservationWiringStatus,
  useRetryReservationWiring,
  retryReservationWiring,
  summarizeWiringRetry,
  wiringStatusKey,
} from "@/api/reservations";
import type { WiringRetryResponse } from "@/types/reservation.types";

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

const RES_ID = "11111111-1111-1111-1111-111111111111";
const SW_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

const STATUS = {
  reservation_id: RES_ID,
  last_applied_fork_version: 3,
  frozen: false,
  connections: [
    {
      id: "c-1",
      switch_device_id: SW_ID,
      port_a: "et1",
      port_b: "et2",
      physical_connection_id: "p-1",
      status: "ACTIVE",
      attempts: 1,
      last_error: null,
      retryable: false,
      created_at: "2026-07-01T00:00:00Z",
      released_at: null,
    },
    {
      id: "c-2",
      switch_device_id: SW_ID,
      port_a: "et3",
      port_b: "et4",
      physical_connection_id: null,
      status: "FAILED",
      attempts: 4,
      last_error: "driver timeout",
      retryable: true,
      created_at: "2026-07-01T00:00:00Z",
      released_at: null,
    },
  ],
};

describe("reservation wiring status api", () => {
  beforeEach(() => {
    toastFn.mockClear();
    toastSuccess.mockClear();
    toastError.mockClear();
  });

  it("useReservationWiringStatus GETs /reservations/{id}/wiring-status", async () => {
    server.use(
      http.get(`/api/reservations/${RES_ID}/wiring-status`, () => HttpResponse.json(STATUS)),
    );
    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={makeClient()}>{children}</QueryClientProvider>;
    }
    const { result } = renderHook(() => useReservationWiringStatus(RES_ID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.last_applied_fork_version).toBe(3);
    expect(result.current.data?.connections).toHaveLength(2);
  });

  it("useReservationWiringStatus stays disabled with no reservation id", () => {
    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={makeClient()}>{children}</QueryClientProvider>;
    }
    const { result } = renderHook(() => useReservationWiringStatus(null), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("retryReservationWiring POSTs to the retry endpoint and returns outcomes", async () => {
    let method = "";
    server.use(
      http.post(`/api/reservations/${RES_ID}/wiring/retry`, ({ request }) => {
        method = request.method;
        return HttpResponse.json({
          reservation_id: RES_ID,
          results: [
            {
              id: "c-2",
              switch_device_id: SW_ID,
              port_a: "et3",
              port_b: "et4",
              physical_connection_id: null,
              outcome: "reconnected",
              status: "ACTIVE",
              attempts: 5,
              last_error: null,
            },
          ],
        });
      }),
    );
    const result = await retryReservationWiring(RES_ID);
    expect(method).toBe("POST");
    expect(result.results[0].outcome).toBe("reconnected");
  });

  it("summarizeWiringRetry tallies the three outcome kinds", () => {
    const resp: WiringRetryResponse = {
      reservation_id: RES_ID,
      results: [
        { id: "a", switch_device_id: SW_ID, port_a: "1", port_b: "2", physical_connection_id: null, outcome: "reconnected", status: "ACTIVE", attempts: 2, last_error: null },
        { id: "b", switch_device_id: SW_ID, port_a: "3", port_b: "4", physical_connection_id: null, outcome: "still_failed", status: "FAILED", attempts: 3, last_error: "x" },
        { id: "c", switch_device_id: SW_ID, port_a: "5", port_b: "6", physical_connection_id: null, outcome: "not_retryable", status: "FAILED", attempts: 1, last_error: "recorded hop unresolvable" },
        { id: "d", switch_device_id: SW_ID, port_a: "7", port_b: "8", physical_connection_id: null, outcome: "reconnected", status: "ACTIVE", attempts: 2, last_error: null },
      ],
    };
    expect(summarizeWiringRetry(resp)).toEqual({ reconnected: 2, still_failed: 1, not_retryable: 1 });
  });

  it("useRetryReservationWiring toasts a success summary and invalidates the panel query", async () => {
    server.use(
      http.post(`/api/reservations/${RES_ID}/wiring/retry`, () =>
        HttpResponse.json({
          reservation_id: RES_ID,
          results: [
            { id: "c-2", switch_device_id: SW_ID, port_a: "et3", port_b: "et4", physical_connection_id: null, outcome: "reconnected", status: "ACTIVE", attempts: 5, last_error: null },
          ],
        }),
      ),
    );
    const client = makeClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    const { result } = renderHook(() => useRetryReservationWiring(), { wrapper });
    result.current.mutate(RES_ID);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // All-recovered path uses the success toast with the count summary.
    expect(toastSuccess).toHaveBeenCalledTimes(1);
    expect(toastSuccess.mock.calls[0][0]).toContain("1 reconnected");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: wiringStatusKey(RES_ID) });
  });

  it("useRetryReservationWiring uses the neutral toast when some rows did not recover", async () => {
    server.use(
      http.post(`/api/reservations/${RES_ID}/wiring/retry`, () =>
        HttpResponse.json({
          reservation_id: RES_ID,
          results: [
            { id: "c-2", switch_device_id: SW_ID, port_a: "et3", port_b: "et4", physical_connection_id: null, outcome: "still_failed", status: "FAILED", attempts: 6, last_error: "driver timeout" },
          ],
        }),
      ),
    );
    const client = makeClient();
    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    const { result } = renderHook(() => useRetryReservationWiring(), { wrapper });
    result.current.mutate(RES_ID);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(toastFn).toHaveBeenCalledTimes(1);
    expect(toastFn.mock.calls[0][0]).toContain("1 still failed");
  });

  it("useRetryReservationWiring toasts the error detail on failure", async () => {
    server.use(
      http.post(`/api/reservations/${RES_ID}/wiring/retry`, () =>
        HttpResponse.json({ detail: "Reservation wiring is frozen; retry is not allowed" }, { status: 409 }),
      ),
    );
    const client = makeClient();
    function wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    const { result } = renderHook(() => useRetryReservationWiring(), { wrapper });
    result.current.mutate(RES_ID);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(toastError).toHaveBeenCalledWith("Reservation wiring is frozen; retry is not allowed");
  });
});
