import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({ default: { error: toastError, success: toastSuccess } }));

import { server } from "../mocks/server";
import { UserManagementTable } from "@/components/admin/UserManagementTable";
import type { User } from "@/types/auth.types";

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
});

function user(overrides: Partial<User>): User {
  return {
    id: "u-1",
    email: "u1@login.example.com",
    username: "user1",
    full_name: "User One",
    contact_email: null,
    phone: null,
    is_active: true,
    role: "user",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const USERS = [
  user({}),
  user({ id: "root-1", username: "root", full_name: "Root Admin", role: "superadmin" }),
];

function renderTable(client?: QueryClient) {
  const qc =
    client ??
    new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <UserManagementTable currentUserId="admin-1" showRoleControls={false} />
    </QueryClientProvider>,
  );
}

function listHandler(items: User[] = USERS) {
  return http.get("/api/auth/users", () =>
    HttpResponse.json({ items, total: items.length, skip: 0, limit: 50 }),
  );
}

describe("UserManagementTable profile column (issue #1252)", () => {
  it("shows a Name column and an Edit profile control on every row, the superadmin's included", async () => {
    server.use(listHandler());
    renderTable();
    expect(await screen.findByRole("cell", { name: "User One" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Root Admin" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit profile of user1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit profile of root" })).toBeInTheDocument();
  });

  it("PATCHes the chosen account's changed fields and refetches the list", async () => {
    let captured: { url: string; body: unknown } | null = null;
    let listCalls = 0;
    server.use(
      http.get("/api/auth/users", () => {
        listCalls += 1;
        return HttpResponse.json({ items: USERS, total: 2, skip: 0, limit: 50 });
      }),
      http.patch("/api/auth/users/:id/profile", async ({ request }) => {
        captured = { url: new URL(request.url).pathname, body: await request.json() };
        return HttpResponse.json({ ...USERS[1], phone: "555 0199" });
      }),
    );
    renderTable();
    fireEvent.click(await screen.findByRole("button", { name: "Edit profile of root" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Edit profile" })).toBeInTheDocument();
    expect((within(dialog).getByLabelText("Name") as HTMLInputElement).value).toBe("Root Admin");
    fireEvent.change(within(dialog).getByLabelText("Phone"), { target: { value: "555 0199" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Profile saved"));
    expect(captured).toEqual({ url: "/api/auth/users/root-1/profile", body: { phone: "555 0199" } });
    await waitFor(() => expect(listCalls).toBe(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the dialog open and toasts the field error on a 422", async () => {
    server.use(
      listHandler(),
      http.patch("/api/auth/users/:id/profile", () =>
        HttpResponse.json(
          {
            detail: [
              {
                type: "value_error",
                loc: ["body", "contact_email"],
                msg: "value is not a valid email address: An email address must have an @-sign.",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderTable();
    fireEvent.click(await screen.findByRole("button", { name: "Edit profile of user1" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Contact email"), {
      target: { value: "nope" },
    });
    fireEvent.submit(within(dialog).getByLabelText("Contact email").closest("form")!);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Contact email: value is not a valid email address: An email address must have an @-sign.",
      ),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Cancel closes the dialog without a request", async () => {
    let patched = false;
    server.use(
      listHandler(),
      http.patch("/api/auth/users/:id/profile", () => {
        patched = true;
        return HttpResponse.json(USERS[0]);
      }),
    );
    renderTable();
    fireEvent.click(await screen.findByRole("button", { name: "Edit profile of user1" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(patched).toBe(false);
  });

  it("an admin editing their own row also refreshes the cached current user", async () => {
    const self = user({ id: "admin-1", username: "adm", full_name: "Adm", role: "admin" });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    client.setQueryData(["currentUser"], self);
    server.use(
      listHandler([self]),
      http.patch("/api/auth/users/admin-1/profile", () =>
        HttpResponse.json({ ...self, full_name: "Admin Person" }),
      ),
    );
    renderTable(client);
    fireEvent.click(await screen.findByRole("button", { name: "Edit profile of adm" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Admin Person" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save profile" }));
    await waitFor(() =>
      expect(client.getQueryData<User>(["currentUser"])?.full_name).toBe("Admin Person"),
    );
  });
});
