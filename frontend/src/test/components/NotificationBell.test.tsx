import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

  describe("item controls (issue #1076)", () => {
    const LIST_URL = "/api/notifications/notifications";
    const UNREAD = {
      id: "11111111-1111-1111-1111-111111111111",
      user_id: "22222222-2222-2222-2222-222222222222",
      event_type: "reservation.created",
      title: "Reservation confirmed",
      body: "Test body",
      data: {},
      read_at: null,
      created_at: new Date().toISOString(),
    };
    const READ = {
      ...UNREAD,
      id: "33333333-3333-3333-3333-333333333333",
      title: "Reservation ended",
      body: "Older body",
      read_at: new Date().toISOString(),
    };

    function serveList(calls: { patched: string[]; deleted: string[] }) {
      server.use(
        http.get(`${LIST_URL}/unread-count`, () => HttpResponse.json({ count: 1 })),
        http.get(LIST_URL, () =>
          HttpResponse.json({ items: [UNREAD, READ], total: 2, unread: 1 }),
        ),
        http.patch(`${LIST_URL}/:id/read`, ({ params }) => {
          calls.patched.push(params.id as string);
          return HttpResponse.json({ ...UNREAD, read_at: new Date().toISOString() });
        }),
        http.delete(`${LIST_URL}/:id`, ({ params }) => {
          calls.deleted.push(params.id as string);
          return new HttpResponse(null, { status: 204 });
        }),
      );
    }

    async function openPanel() {
      render(<NotificationBell />, { wrapper });
      fireEvent.click(await screen.findByLabelText("Notifications"));
      await screen.findByText("Reservation confirmed");
    }

    function itemFor(title: string): HTMLElement {
      return screen.getByText(title).closest("li") as HTMLElement;
    }

    it("makes no list request while signed out", async () => {
      useAuthStore.setState({
        accessToken: null,
        refreshToken: null,
        user: null,
        isAuthenticated: false,
      });
      let listRequests = 0;
      let countRequests = 0;
      server.use(
        http.get(`${LIST_URL}/unread-count`, () => {
          countRequests += 1;
          return HttpResponse.json({ count: 0 });
        }),
        http.get(LIST_URL, () => {
          listRequests += 1;
          return HttpResponse.json({ items: [], total: 0, unread: 0 });
        }),
      );
      render(<NotificationBell />, { wrapper });
      // Give a query that should not run every chance to fire.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(listRequests).toBe(0);
      expect(countRequests).toBe(0);
    });

    it("clicking an unread notification marks it read through the API", async () => {
      const calls = { patched: [] as string[], deleted: [] as string[] };
      serveList(calls);
      const user = userEvent.setup();
      await openPanel();
      await user.click(
        within(itemFor("Reservation confirmed")).getByRole("button", {
          name: /Reservation confirmed/,
        }),
      );
      await waitFor(() => expect(calls.patched).toEqual([UNREAD.id]));
      expect(calls.deleted).toEqual([]);
    });

    it("clicking a read notification sends nothing", async () => {
      const calls = { patched: [] as string[], deleted: [] as string[] };
      serveList(calls);
      const user = userEvent.setup();
      await openPanel();
      await user.click(
        within(itemFor("Reservation ended")).getByRole("button", { name: /Reservation ended/ }),
      );
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(calls.patched).toEqual([]);
    });

    it("Delete removes that notification through the API and does not mark it read", async () => {
      const calls = { patched: [] as string[], deleted: [] as string[] };
      serveList(calls);
      const user = userEvent.setup();
      await openPanel();
      await user.click(
        within(itemFor("Reservation confirmed")).getByRole("button", {
          name: "Delete notification",
        }),
      );
      await waitFor(() => expect(calls.deleted).toEqual([UNREAD.id]));
      expect(calls.patched).toEqual([]);
    });

    it("the item and its Delete are separate keyboard stops with their own names", async () => {
      const calls = { patched: [] as string[], deleted: [] as string[] };
      serveList(calls);
      const user = userEvent.setup();
      await openPanel();
      const item = itemFor("Reservation confirmed");
      const buttons = within(item).getAllByRole("button");
      expect(buttons).toHaveLength(2);
      const [primary, remove] = buttons;
      expect(primary).toHaveAccessibleName(/Reservation confirmed/);
      expect(primary).not.toHaveAccessibleName(/Delete notification/);
      expect(remove).toHaveAccessibleName("Delete notification");
      expect(primary.contains(remove)).toBe(false);

      primary.focus();
      await user.tab();
      expect(remove).toHaveFocus();
      await user.keyboard("{Enter}");
      await waitFor(() => expect(calls.deleted).toEqual([UNREAD.id]));

      primary.focus();
      await user.keyboard("{Enter}");
      await waitFor(() => expect(calls.patched).toEqual([UNREAD.id]));
    });

    it("Mark all read is disabled while nothing is unread", async () => {
      server.use(
        http.get(`${LIST_URL}/unread-count`, () => HttpResponse.json({ count: 0 })),
        http.get(LIST_URL, () => HttpResponse.json({ items: [READ], total: 1, unread: 0 })),
      );
      render(<NotificationBell />, { wrapper });
      fireEvent.click(await screen.findByLabelText("Notifications"));
      await screen.findByText("Reservation ended");
      expect(screen.getByRole("button", { name: "Mark all read" })).toBeDisabled();
    });

    it("Mark all read is enabled while something is unread", async () => {
      serveList({ patched: [], deleted: [] });
      await openPanel();
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Mark all read" })).toBeEnabled(),
      );
    });

    it("never nests a button inside a button", async () => {
      serveList({ patched: [], deleted: [] });
      await openPanel();
      const buttons = screen.getAllByRole("button");
      expect(buttons.length).toBeGreaterThan(3);
      for (const button of buttons) {
        expect(button.parentElement?.closest("button") ?? null).toBeNull();
      }
    });
  });
});
