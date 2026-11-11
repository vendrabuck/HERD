import { http, HttpResponse } from "msw";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import toast, { Toaster } from "react-hot-toast";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { server } from "../mocks/server";
import { GroupsPage } from "@/pages/admin/GroupsPage";

// Issues #1314 and #1344: a toast path fed FastAPI's 422 list. The REAL
// react-hot-toast Toaster renders here (GroupsPage.test.tsx mocks it), because
// the crash lived in the Toaster: it rendered the list as a React child and
// threw outside every error boundary. errorDetail is the fix at the source.

// jsdom has no matchMedia; react-hot-toast reads prefers-reduced-motion
// through it while positioning a toast (the same stub as App.test.tsx).
beforeAll(() => {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as MediaQueryList,
  );
});

afterAll(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  act(() => {
    toast.remove();
  });
});

const MSG = "Input should be a valid UUID";

describe("GroupsPage delete refusal toast (issue #1314)", () => {
  it("renders a 422 validation list as readable toast text, the page still mounted", async () => {
    server.use(
      http.get("/api/auth/groups", () =>
        HttpResponse.json({
          items: [
            {
              id: "g-1",
              name: "Network Team",
              description: null,
              created_by: null,
              created_at: "2026-01-05T00:00:00Z",
            },
          ],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
      http.delete("/api/auth/groups/g-1", () =>
        HttpResponse.json(
          { detail: [{ type: "uuid_parsing", loc: ["path", "group_id"], msg: MSG, input: "g-1" }] },
          { status: 422 },
        ),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Toaster />
          <GroupsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("Network Team");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Group" }));
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("group_id: " + MSG);
    // The page is still there; nothing unmounted the tree.
    expect(screen.getByText("Network Team")).toBeInTheDocument();
  });
});
