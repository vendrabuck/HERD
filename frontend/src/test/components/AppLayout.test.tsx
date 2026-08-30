import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, it, expect, beforeEach } from "vitest";

import { server } from "../mocks/server";
import { AppLayout } from "@/components/layout/AppLayout";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import type { User } from "@/types/auth.types";

function user(overrides: Partial<User> = {}): User {
  return {
    id: "u-1",
    email: "a@b.com",
    username: "alice",
    is_active: true,
    role: "user",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderLayout(initialEntry = "/inventory") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="*" element={<div>page content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// Reports the router's current location so a test can assert navigation
// happened, without relying on renderLayout's static "page content" stub.
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

function renderLayoutWithLocationProbe(initialEntry = "/inventory") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="*" element={<LocationProbe />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const ADMIN_DROPDOWN_ENTRIES: [string, string][] = [
  ["/admin/add-device", "Add Device"],
  ["/admin/groups", "User Groups"],
  ["/admin/device-groups", "Device Groups"],
  ["/admin/connections", "Connections"],
  ["/admin/drivers", "Drivers"],
  ["/admin/grants", "Grants"],
  ["/admin/hypervisors", "Hypervisors"],
  ["/admin/ldap-sync", "LDAP Sync"],
  ["/admin/users", "Users"],
];

beforeEach(() => {
  useAuthStore.setState({
    accessToken: "t",
    refreshToken: "r",
    user: null,
    isAuthenticated: true,
  });
  usePreferencesStore.setState({
    savedFilters: {},
    pageSizes: {},
    extras: {},
    loaded: false,
  });
  server.use(
    http.get("/api/notifications/notifications/unread-count", () =>
      HttpResponse.json({ count: 0 }),
    ),
    http.get("/api/notifications/notifications", () =>
      HttpResponse.json({ items: [], total: 0 }),
    ),
    http.get("/api/user-profile/preferences", () =>
      HttpResponse.json({ saved_filters: {}, page_sizes: {}, extras: {} }),
    ),
  );
});

describe("AppLayout", () => {
  it("renders the primary nav items and outlet content", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayout("/inventory");

    await screen.findByText("alice");
    for (const label of ["Inventory", "Templates", "Topology", "Reservations", "Reporting"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByText("page content")).toBeInTheDocument();
  });

  it("does not render the Administration menu for a plain user", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "user" }))));
    renderLayout("/inventory");

    await screen.findByText("alice");
    expect(screen.queryByRole("button", { name: "Administration" })).not.toBeInTheDocument();
  });

  it("renders the Administration menu for an admin user and reveals items on hover", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "admin" }))));
    renderLayout("/inventory");

    await screen.findByText("alice");
    const adminButton = screen.getByRole("button", { name: "Administration" });
    expect(adminButton).toBeInTheDocument();

    // Dropdown items are not present until the trigger is hovered.
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    fireEvent.mouseEnter(adminButton.parentElement as HTMLElement);
    expect(screen.getByRole("link", { name: "Users" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "LDAP Sync" })).toBeInTheDocument();

    fireEvent.mouseLeave(adminButton.parentElement as HTMLElement);
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
  });

  it("highlights each admin dropdown item as active on its own route", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "admin" }))));
    const adminRoutes = ADMIN_DROPDOWN_ENTRIES;

    for (const [path, label] of adminRoutes) {
      const { unmount } = renderLayout(path);
      await screen.findByText("alice");
      const adminButton = screen.getByRole("button", { name: "Administration" });
      fireEvent.mouseEnter(adminButton.parentElement as HTMLElement);
      const link = screen.getByRole("link", { name: label });
      expect(link.className).toContain("bg-gray-700 text-white");
      // A sibling item on the same dropdown stays inactive (only the
      // hover:bg-gray-700 variant, never the plain active class).
      const otherLabel = adminRoutes.find(([, l]) => l !== label)![1];
      const other = screen.getByRole("link", { name: otherLabel });
      expect(other.className).not.toContain("bg-gray-700 text-white");
      unmount();
    }
  });

  it.each(ADMIN_DROPDOWN_ENTRIES)(
    "clicking the %s dropdown entry (%s) closes the dropdown and navigates to it",
    async (path, label) => {
      server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "admin" }))));
      renderLayoutWithLocationProbe("/inventory");

      await screen.findByText("alice");
      const adminButton = screen.getByRole("button", { name: "Administration" });
      fireEvent.mouseEnter(adminButton.parentElement as HTMLElement);
      const entry = screen.getByRole("link", { name: label });
      fireEvent.click(entry);

      // The onClick={() => setAdminOpen(false)} handler on this NavLink
      // closes the dropdown: its own entry (and every sibling entry, since
      // the whole dropdown content unmounts) is gone from the document.
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
      // The click also navigated: the router's location moved to this
      // entry's href.
      await waitFor(() =>
        expect(screen.getByTestId("location-probe")).toHaveTextContent(path),
      );
    },
  );

  it("also renders the Administration menu for superadmin (case-sensitive isAdminRole)", async () => {
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json(user({ role: "superadmin" }))),
    );
    renderLayout("/inventory");

    await screen.findByText("alice");
    expect(screen.getByRole("button", { name: "Administration" })).toBeInTheDocument();
  });

  it("marks the Reservations nav link active for a nested reservations path", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayout("/reservations/res-1");

    await screen.findByText("alice");
    const reservationsLink = screen.getByRole("link", { name: "Reservations" });
    expect(reservationsLink.className).toContain("bg-gray-700");
  });

  it("does not mark Inventory active while on a reservations sub-path", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayout("/reservations/res-1");

    await screen.findByText("alice");
    const inventoryLink = screen.getByRole("link", { name: "Inventory" });
    expect(inventoryLink.className).not.toContain("bg-gray-700");
  });

  it("logs out, clears preferences, and navigates to /login", async () => {
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json(user())),
      http.post("/api/auth/logout", () => new HttpResponse(null, { status: 204 })),
    );
    renderLayout("/inventory");

    await screen.findByText("alice");
    fireEvent.click(screen.getByRole("button", { name: "Logout" }));

    await waitFor(() => expect(screen.getByText("page content")).toBeInTheDocument());
    // Logout clears auth (mirrors real navigation to /login, verified by
    // isAuthenticated flipping so a re-render would blank currentUser).
    await waitFor(() => expect(useAuthStore.getState().isAuthenticated).toBe(false));
    expect(usePreferencesStore.getState().loaded).toBe(false);
  });

  it("disables the Logout button while the logout request is pending", async () => {
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json(user())),
      http.post(
        "/api/auth/logout",
        () => new Promise(() => {}), // never resolves, keeps mutation pending
      ),
    );
    renderLayout("/inventory");

    await screen.findByText("alice");
    const logoutButton = screen.getByRole("button", { name: "Logout" });
    fireEvent.click(logoutButton);

    await waitFor(() => expect(logoutButton).toBeDisabled());
  });

  it("renders the notification bell for an authenticated user", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayout("/inventory");

    await screen.findByText("alice");
    // NotificationBell renders its own bell button once authenticated.
    expect(screen.getByRole("button", { name: /notifications/i })).toBeInTheDocument();
  });

  it("does not render a username when no user has loaded yet", () => {
    // No /auth/me handler registered: useCurrentUser stays in a pending
    // state, so `user` is undefined and the username span never renders.
    renderLayout("/inventory");
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
  });

  it("renders a Settings link and a help link to the user guide", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayout("/inventory");

    await screen.findByText("alice");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    const help = screen.getByRole("link", { name: "Help" });
    expect(help).toHaveAttribute(
      "href",
      "https://github.com/vendrabuck/HERD/blob/main/docs/USER_GUIDE.md",
    );
    expect(help).toHaveAttribute("target", "_blank");
  });

  it("closes the admin dropdown after clicking an admin item", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "admin" }))));
    renderLayout("/inventory");

    await screen.findByText("alice");
    const adminButton = screen.getByRole("button", { name: "Administration" });
    fireEvent.mouseEnter(adminButton.parentElement as HTMLElement);
    const usersLink = screen.getByRole("link", { name: "Users" });
    fireEvent.click(usersLink);
    // The click handler closes the dropdown synchronously.
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
  });
});
