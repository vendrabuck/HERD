import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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

import { AdminGuard } from "@/App";
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
});
