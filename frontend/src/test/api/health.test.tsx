import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "../mocks/server";
import { useDeviceHealth, useDeviceHealthList } from "@/api/health";

function Wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("health API client", () => {
  it("useDeviceHealth fetches a snapshot for a device", async () => {
    server.use(
      http.get("/api/execution/device-health/d1", () =>
        HttpResponse.json({
          device_id: "d1",
          last_polled_at: "2026-05-26T10:00:00Z",
          last_status: "HEALTHY",
          last_run_id: "run-1",
          consecutive_failures: 0,
          next_poll_at: "2026-05-26T10:01:00Z",
        }),
      ),
    );

    function Probe() {
      const { data } = useDeviceHealth("d1");
      return <span data-testid="status">{data?.last_status ?? ""}</span>;
    }

    render(
      <Wrapper>
        <Probe />
      </Wrapper>,
    );

    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("HEALTHY"));
  });

  it("useDeviceHealth is disabled when device_id is null", () => {
    function Probe() {
      const { fetchStatus } = useDeviceHealth(null);
      return <span data-testid="fetch-status">{fetchStatus}</span>;
    }
    const { getByTestId } = render(
      <Wrapper>
        <Probe />
      </Wrapper>,
    );
    expect(getByTestId("fetch-status").textContent).toBe("idle");
  });

  it("useDeviceHealthList fetches paginated health rows", async () => {
    server.use(
      http.get("/api/execution/device-health", () =>
        HttpResponse.json({
          items: [
            {
              device_id: "d1",
              last_polled_at: null,
              last_status: "UNKNOWN",
              last_run_id: null,
              consecutive_failures: 0,
              next_poll_at: null,
            },
          ],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );

    function Probe() {
      const { data } = useDeviceHealthList();
      return <span data-testid="total">{data?.total ?? ""}</span>;
    }

    render(
      <Wrapper>
        <Probe />
      </Wrapper>,
    );

    await waitFor(() => expect(screen.getByTestId("total").textContent).toBe("1"));
  });
});
