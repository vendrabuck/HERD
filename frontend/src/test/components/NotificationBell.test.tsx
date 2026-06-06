import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, beforeEach } from "vitest";

import { server } from "../mocks/server";
import { NotificationBell } from "@/components/NotificationBell";
import { useAuthStore } from "@/stores/authStore";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  useAuthStore.setState({
    accessToken: "t",
    refreshToken: "r",
    user: null,
    isAuthenticated: true,
  });
});

describe("NotificationBell", () => {
  it("returns null when not authenticated", () => {
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
    });
    const { container } = render(<NotificationBell />, { wrapper });
    expect(container.firstChild).toBeNull();
  });

  it("shows unread badge when count > 0", async () => {
    server.use(
      http.get("/api/notifications/notifications/unread-count", () =>
        HttpResponse.json({ count: 3 }),
      ),
      http.get("/api/notifications/notifications", () =>
        HttpResponse.json({ items: [], total: 0, unread: 0 }),
      ),
    );

    render(<NotificationBell />, { wrapper });
    const badge = await screen.findByText("3");
    expect(badge).toBeInTheDocument();
  });

  it("opens the panel and lists notifications", async () => {
    server.use(
      http.get("/api/notifications/notifications/unread-count", () =>
        HttpResponse.json({ count: 1 }),
      ),
      http.get("/api/notifications/notifications", () =>
        HttpResponse.json({
          items: [
            {
              id: "11111111-1111-1111-1111-111111111111",
              user_id: "22222222-2222-2222-2222-222222222222",
              event_type: "reservation.created",
              title: "Reservation confirmed",
              body: "Test body",
              data: {},
              read_at: null,
              created_at: new Date().toISOString(),
            },
          ],
          total: 1,
          unread: 1,
        }),
      ),
    );

    render(<NotificationBell />, { wrapper });
    const button = await screen.findByLabelText("Notifications");
    fireEvent.click(button);
    await waitFor(() =>
      expect(screen.getByText("Reservation confirmed")).toBeInTheDocument(),
    );
    expect(screen.getByText("Test body")).toBeInTheDocument();
  });

  it("shows empty state when there are no notifications", async () => {
    server.use(
      http.get("/api/notifications/notifications/unread-count", () =>
        HttpResponse.json({ count: 0 }),
      ),
      http.get("/api/notifications/notifications", () =>
        HttpResponse.json({ items: [], total: 0, unread: 0 }),
      ),
    );

    render(<NotificationBell />, { wrapper });
    fireEvent.click(await screen.findByLabelText("Notifications"));
    await waitFor(() =>
      expect(screen.getByText(/No notifications yet/i)).toBeInTheDocument(),
    );
  });
});
