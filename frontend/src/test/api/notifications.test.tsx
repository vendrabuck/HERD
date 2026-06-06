import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useNotifications,
  useNotificationPreferences,
  useUnreadCount,
} from "@/api/notifications";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("notifications api hooks", () => {
  it("useUnreadCount returns the count", async () => {
    server.use(
      http.get("/api/notifications/notifications/unread-count", () =>
        HttpResponse.json({ count: 7 }),
      ),
    );

    const { result } = renderHook(() => useUnreadCount(true, 0), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBe(7);
  });

  it("useUnreadCount is disabled when not authenticated", () => {
    const { result } = renderHook(() => useUnreadCount(false, 0), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useNotifications returns the items list", async () => {
    const payload = {
      items: [
        {
          id: "11111111-1111-1111-1111-111111111111",
          user_id: "22222222-2222-2222-2222-222222222222",
          event_type: "reservation.created",
          title: "Reservation confirmed",
          body: "body",
          data: {},
          read_at: null,
          created_at: "2026-04-20T00:00:00+00:00",
        },
      ],
      total: 1,
      unread: 1,
    };
    server.use(
      http.get("/api/notifications/notifications", () => HttpResponse.json(payload)),
    );

    const { result } = renderHook(() => useNotifications({ limit: 20 }), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toHaveLength(1);
    expect(result.current.data?.unread).toBe(1);
  });

  it("useNotificationPreferences returns the prefs shape", async () => {
    const payload = {
      channels: { in_app: true },
      events: { "reservation.created": true, "reservation.completed": false },
    };
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(payload),
      ),
    );

    const { result } = renderHook(() => useNotificationPreferences(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.events["reservation.completed"]).toBe(false);
  });
});
