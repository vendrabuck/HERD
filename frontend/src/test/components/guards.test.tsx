import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, beforeEach } from "vitest";

import { AuthGuard, GuestGuard } from "@/components/guards";
import { useAuthStore } from "@/stores/authStore";

// AuthGuard and GuestGuard redirect declaratively via <Navigate>, unlike
// AdminGuard's useNavigate side effect, so a real MemoryRouter with a
// destination route is enough to observe the redirect: no router mocking
// needed here.
function renderAuthGuardRoute(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/topology"
          element={
            <AuthGuard>
              <p>Protected content</p>
            </AuthGuard>
          }
        />
        <Route path="/login" element={<p>Login page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderGuestGuardRoute(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/login"
          element={
            <GuestGuard>
              <p>Login form</p>
            </GuestGuard>
          }
        />
        <Route path="/topology" element={<p>Topology page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useAuthStore.setState({ isAuthenticated: false });
});

describe("AuthGuard", () => {
  it("redirects an unauthenticated user to /login", () => {
    useAuthStore.setState({ isAuthenticated: false });

    renderAuthGuardRoute("/topology");

    expect(screen.getByText("Login page")).toBeInTheDocument();
    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();
  });

  it("renders children for an authenticated user", () => {
    useAuthStore.setState({ isAuthenticated: true });

    renderAuthGuardRoute("/topology");

    expect(screen.getByText("Protected content")).toBeInTheDocument();
    expect(screen.queryByText("Login page")).not.toBeInTheDocument();
  });
});

describe("GuestGuard", () => {
  it("redirects an authenticated user to /topology", () => {
    useAuthStore.setState({ isAuthenticated: true });

    renderGuestGuardRoute("/login");

    expect(screen.getByText("Topology page")).toBeInTheDocument();
    expect(screen.queryByText("Login form")).not.toBeInTheDocument();
  });

  it("renders children for an unauthenticated user", () => {
    useAuthStore.setState({ isAuthenticated: false });

    renderGuestGuardRoute("/login");

    expect(screen.getByText("Login form")).toBeInTheDocument();
    expect(screen.queryByText("Topology page")).not.toBeInTheDocument();
  });
});
