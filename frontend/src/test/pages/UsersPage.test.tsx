import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { UsersPage } from "@/pages/admin/UsersPage";
import { useAuthStore } from "@/stores/authStore";

// UserManagementTable owns its own data fetching and rendering and has its
// own test coverage; stub it here so this page's test stays scoped to the
// page's own logic: the null guard and the showRoleControls/currentUserId
// wiring it passes down.
const tableProps = vi.hoisted(() => ({ current: null as unknown }));
vi.mock("@/components/admin/UserManagementTable", () => ({
  UserManagementTable: (props: unknown) => {
    tableProps.current = props;
    return <div data-testid="user-management-table" />;
  },
}));

describe("UsersPage", () => {
  beforeEach(() => {
    tableProps.current = null;
  });

  it("renders nothing while there is no authenticated user", () => {
    useAuthStore.setState({ user: null } as never);
    const { container } = render(<UsersPage />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the heading and table for a regular admin without role controls", () => {
    useAuthStore.setState({
      user: { id: "u-1", username: "admin1", email: "a@b.c", role: "admin" },
    } as never);
    render(<UsersPage />);

    expect(screen.getByRole("heading", { name: "User Management" })).toBeInTheDocument();
    expect(screen.getByTestId("user-management-table")).toBeInTheDocument();
    expect(tableProps.current).toEqual({
      currentUserId: "u-1",
      showRoleControls: false,
    });
  });

  it("enables role controls only for a superadmin", () => {
    useAuthStore.setState({
      user: { id: "u-2", username: "root", email: "r@b.c", role: "superadmin" },
    } as never);
    render(<UsersPage />);

    expect(tableProps.current).toEqual({
      currentUserId: "u-2",
      showRoleControls: true,
    });
  });
});
