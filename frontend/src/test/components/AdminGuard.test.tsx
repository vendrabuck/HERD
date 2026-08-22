import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";

// useNavigate is a side effect we want to observe directly, so keep the real
// MemoryRouter for everything else and only stub the hook.
const navigateSpy = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateSpy,
  };
});

import { AdminGuard } from "@/components/guards";
import { useAuthStore } from "@/stores/authStore";

function renderGuard() {
  return render(
    <MemoryRouter>
      <AdminGuard>
        <p>Admin-only content</p>
      </AdminGuard>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  navigateSpy.mockClear();
  useAuthStore.setState({ user: null });
});

describe("AdminGuard", () => {
  it("redirects a non-admin user to /topology and renders nothing", async () => {
    useAuthStore.setState({
      user: { id: "1", username: "bob", email: "b@b.c", role: "user", is_active: true, created_at: "" },
    });

    renderGuard();

    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith("/topology"));
    expect(screen.queryByText("Admin-only content")).not.toBeInTheDocument();
  });

  it("renders children for an admin user", () => {
    useAuthStore.setState({
      user: { id: "1", username: "admin", email: "a@b.c", role: "admin", is_active: true, created_at: "" },
    });

    renderGuard();

    expect(screen.getByText("Admin-only content")).toBeInTheDocument();
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("renders children for a superadmin user", () => {
    useAuthStore.setState({
      user: {
        id: "1",
        username: "super",
        email: "s@b.c",
        role: "superadmin",
        is_active: true,
        created_at: "",
      },
    });

    renderGuard();

    expect(screen.getByText("Admin-only content")).toBeInTheDocument();
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("renders nothing without crashing while unauthenticated", () => {
    useAuthStore.setState({ user: null });

    renderGuard();

    expect(screen.queryByText("Admin-only content")).not.toBeInTheDocument();
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  // Issue #548: /reporting sits outside pages/admin/ but is routed through
  // the same AdminGuard-wrapped route group in App.tsx as the admin pages.
  // This mirrors that exact nesting (an AdminGuard-wrapped pathless Route
  // with an Outlet, holding a /reporting child) rather than re-rendering the
  // full App tree: App's default export hardcodes BrowserRouter (so it
  // cannot be seeded with initialEntries) and pulls in AppLayout plus every
  // other page's own API calls, none of which are mocked in this file.
  function renderReportingRoute() {
    return render(
      <MemoryRouter initialEntries={["/reporting"]}>
        <Routes>
          <Route
            element={
              <AdminGuard>
                <Outlet />
              </AdminGuard>
            }
          >
            <Route path="/reporting" element={<p>Reporting content</p>} />
          </Route>
          <Route path="/topology" element={<p>Topology content</p>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("guards the /reporting route and redirects a non-admin to /topology", async () => {
    useAuthStore.setState({
      user: { id: "1", username: "bob", email: "b@b.c", role: "user", is_active: true, created_at: "" },
    });

    renderReportingRoute();

    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith("/topology"));
    expect(screen.queryByText("Reporting content")).not.toBeInTheDocument();
  });

  it("renders the /reporting route for an admin", () => {
    useAuthStore.setState({
      user: { id: "1", username: "admin", email: "a@b.c", role: "admin", is_active: true, created_at: "" },
    });

    renderReportingRoute();

    expect(screen.getByText("Reporting content")).toBeInTheDocument();
    expect(navigateSpy).not.toHaveBeenCalled();
  });
});
