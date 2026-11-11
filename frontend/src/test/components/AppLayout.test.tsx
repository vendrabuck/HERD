import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, createMemoryRouter, RouterProvider, useLocation } from "react-router-dom";
import { act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, beforeEach } from "vitest";
import { ADMIN_NAV } from "@/lib/adminNav";

import { server } from "../mocks/server";
import { AppLayout } from "@/components/layout/AppLayout";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import type { User } from "@/types/auth.types";
import { APP_VERSION } from "@/lib/appVersion";

function user(overrides: Partial<User> = {}): User {
  return {
    id: "u-1",
    email: "a@b.com",
    username: "alice",
    full_name: "alice",
    contact_email: null,
    phone: null,
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
  ["/admin/about", "About"],
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

  it("shows the full name in the header (issue #1252)", async () => {
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json(user({ full_name: "Alice Smith" }))),
    );
    renderLayout("/inventory");
    const name = await screen.findByText("Alice Smith");
    expect(name).toHaveAttribute("title", "alice");
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
  });

  it("falls back to the username when the full name is empty (issue #1252)", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ full_name: "" }))));
    renderLayout("/inventory");
    expect(await screen.findByText("alice")).toBeInTheDocument();
  });

  it("shows the injected app version beside the HERD wordmark (issue #846)", () => {
    renderLayout("/inventory");
    expect(screen.getByTestId("app-version-header")).toHaveTextContent(`v${APP_VERSION}`);
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

  it("renders a Settings link and a help link to the published manual", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayout("/inventory");

    await screen.findByText("alice");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    const help = screen.getByRole("link", { name: "Help" });
    // The literal on purpose (issue #960): the help icon must open the published
    // manual, not a raw markdown file on GitHub.
    expect(help).toHaveAttribute("href", "https://vendrabuck.github.io/HERD/manual/");
    expect(help).toHaveAttribute("rel", "noopener noreferrer");
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

function renderLayoutInDataRouter(initialEntry = "/inventory") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  const router = createMemoryRouter(
    [{ element: <AppLayout />, children: [{ path: "*", element: <LocationProbe /> }] }],
    { initialEntries: [initialEntry] },
  );
  const utils = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...utils, router };
}

describe("AppLayout Administration disclosure, keyboard (issue #1317)", () => {
  async function adminButton() {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "admin" }))));
    const utils = renderLayoutInDataRouter("/inventory");
    await screen.findByText("alice");
    return { ...utils, button: screen.getByRole("button", { name: "Administration" }) };
  }

  it("is a disclosure button: collapsed, controlling a hidden list of every ADMIN_NAV link", async () => {
    const { button } = await adminButton();
    expect(button).toHaveAttribute("aria-expanded", "false");
    // A disclosure, not an ARIA menu: no menu role and no aria-haspopup.
    expect(button).not.toHaveAttribute("aria-haspopup");
    expect(screen.queryByRole("menu")).toBe(null);
    const panel = document.getElementById(button.getAttribute("aria-controls") ?? "");
    expect(panel).not.toBe(null);
    expect(panel).not.toBeVisible();
    // The panel's links come from ADMIN_NAV, in order.
    const hrefs = [...(panel as HTMLElement).querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(ADMIN_NAV.map((item) => item.to));
  });

  it("Tab focuses the button, Enter opens it, Tab walks its links, Escape closes and restores focus", async () => {
    const { button } = await adminButton();
    const kb = userEvent.setup();

    while (document.activeElement !== button) await kb.tab();
    await kb.keyboard("{Enter}");
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "Add Device" })).toBeVisible();

    // The next Tab stops are the links themselves, in ADMIN_NAV order.
    for (const item of ADMIN_NAV.slice(0, 3)) {
      await kb.tab();
      expect(document.activeElement).toBe(screen.getByRole("link", { name: item.label }));
    }

    await kb.keyboard("{Escape}");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "Add Device" })).toBe(null);
    expect(document.activeElement).toBe(button);
  });

  it("Space toggles it open and closed", async () => {
    const { button } = await adminButton();
    const kb = userEvent.setup();
    button.focus();
    await kb.keyboard(" ");
    expect(button).toHaveAttribute("aria-expanded", "true");
    await kb.keyboard(" ");
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("Enter on a link navigates to it and closes the panel", async () => {
    const { button } = await adminButton();
    const kb = userEvent.setup();
    button.focus();
    await kb.keyboard("{Enter}");
    await kb.tab();
    await kb.tab();
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "User Groups" }));
    await kb.keyboard("{Enter}");
    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent("/admin/groups"),
    );
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("closes on a route change made elsewhere", async () => {
    const { button, router } = await adminButton();
    const kb = userEvent.setup();
    button.focus();
    await kb.keyboard("{Enter}");
    expect(button).toHaveAttribute("aria-expanded", "true");

    await act(async () => {
      await router.navigate("/topology");
    });

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/topology");
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("closes on a click outside it", async () => {
    const { button } = await adminButton();
    const kb = userEvent.setup();
    await kb.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    await kb.click(screen.getByTestId("location-probe"));
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("closes when keyboard focus leaves it", async () => {
    const { button } = await adminButton();
    const kb = userEvent.setup();
    button.focus();
    await kb.keyboard("{Enter}");
    // Shift+Tab from the button moves focus to the main nav, outside the disclosure.
    await kb.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Reporting" }));
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("a click while hover-opened keeps it open (the e2e helper hovers, then clicks)", async () => {
    const { button } = await adminButton();
    const kb = userEvent.setup();
    // userEvent.click moves the pointer onto the button first, which fires
    // mouseenter on the wrapper and opens it by hover.
    await kb.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    // Pinned by the click: leaving with the mouse no longer closes it.
    fireEvent.mouseLeave(button.parentElement as HTMLElement);
    expect(button).toHaveAttribute("aria-expanded", "true");
    // A second click closes it.
    await kb.click(button);
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("hover alone opens it and mouse leave closes it, with aria-expanded tracking both", async () => {
    const { button } = await adminButton();
    fireEvent.mouseEnter(button.parentElement as HTMLElement);
    expect(button).toHaveAttribute("aria-expanded", "true");
    fireEvent.mouseLeave(button.parentElement as HTMLElement);
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("is absent for a plain user, and no admin link is in the document", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "user" }))));
    renderLayoutInDataRouter("/inventory");
    await screen.findByText("alice");
    expect(screen.queryByRole("button", { name: "Administration" })).toBe(null);
    for (const item of ADMIN_NAV) {
      expect(document.querySelector(`a[href="${item.to}"]`), item.to).toBe(null);
    }
  });
});

describe("AppLayout landmarks and titles (issue #1317)", () => {
  it("wraps the routed content in a main landmark that the skip link focuses", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayoutInDataRouter("/inventory");
    await screen.findByText("alice");
    const main = screen.getByRole("main");
    expect(main).toContainElement(screen.getByTestId("location-probe"));
    expect(screen.getAllByRole("main")).toHaveLength(1);

    const kb = userEvent.setup();
    await kb.tab();
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    // The skip link is the first Tab stop on the page.
    expect(document.activeElement).toBe(skip);
    expect(skip).toHaveAttribute("href", `#${main.id}`);
    await kb.keyboard("{Enter}");
    expect(document.activeElement).toBe(main);
  });

  it("names the primary navigation", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user())));
    renderLayoutInDataRouter("/inventory");
    await screen.findByText("alice");
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });

  it("sets document.title per route and restores the original title on unmount", async () => {
    document.title = "HERD original";
    server.use(http.get("/api/auth/me", () => HttpResponse.json(user({ role: "admin" }))));
    const { router, unmount } = renderLayoutInDataRouter("/inventory");
    await screen.findByText("alice");
    expect(document.title).toBe("Inventory - HERD");

    await act(async () => {
      await router.navigate("/topology/abc");
    });
    expect(document.title).toBe("Topology editor - HERD");

    await act(async () => {
      await router.navigate("/admin/ldap-sync");
    });
    expect(document.title).toBe("LDAP Sync - HERD");

    await act(async () => {
      await router.navigate("/no-such-page");
    });
    expect(document.title).toBe("HERD original");

    unmount();
    expect(document.title).toBe("HERD original");
  });
});
