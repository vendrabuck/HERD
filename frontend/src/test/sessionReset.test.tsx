import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("react-hot-toast", () => ({ default: { error: vi.fn(), success: vi.fn() } }));

import { server } from "./mocks/server";
import { fakeJwt } from "./fixtures/jwt";
import { resetOnSignOut } from "@/queryClient";
import { AuthGuard, GuestGuard } from "@/components/guards";
import { LoginPage } from "@/pages/LoginPage";
import apiClient from "@/api/client";
import { useLogout } from "@/api/auth";
import { tokenSubject, useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";

/*
 * Issue #1333 review, finding 1: every path into the signed-out state empties
 * the query cache and resets the preferences store (resetOnSignOut, the one
 * subscription beside the app's query client), and a return path recorded
 * for one account is never honored for another. Alice is on her reservation
 * page; she is signed out by each path in turn; bob then signs in in the same
 * tab and must land on /topology without ever seeing alice's data.
 */

const ALICE = fakeJwt("alice", 1);
const BOB = fakeJwt("bob", 1);

function Where() {
  const location = useLocation();
  return <p>At {location.pathname}</p>;
}

function ReservationPage() {
  const { id } = useParams();
  const logout = useLogout();
  const { data } = useQuery({
    queryKey: ["reservation", id],
    queryFn: async () => (await apiClient.get(`/reservations/${id}`)).data as { title: string },
  });
  return (
    <div>
      <Where />
      {data && <p>{data.title}</p>}
      <button type="button" onClick={() => logout.mutate()}>
        Log out
      </button>
      <button type="button" onClick={() => apiClient.get("/thing").catch(() => {})}>
        Load
      </button>
    </div>
  );
}

let client: QueryClient;
let stopReset: () => void;
// Query cache size seen by a subscriber registered after resetOnSignOut, at
// each true-to-false edge of isAuthenticated: zustand calls listeners in
// subscription order, so this is the cache right after the reset ran.
let cacheSizeAtSignOut: number[];
let stopWatch: () => void;

function renderApp() {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/reservations/alice-res-1"]}>
        <Routes>
          <Route
            path="/login"
            element={
              <GuestGuard>
                <LoginPage />
              </GuestGuard>
            }
          />
          <Route
            path="/reservations/:id"
            element={
              <AuthGuard>
                <ReservationPage />
              </AuthGuard>
            }
          />
          <Route
            path="*"
            element={
              <AuthGuard>
                <Where />
              </AuthGuard>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function storageEvent(key: string | null) {
  window.dispatchEvent(new StorageEvent("storage", { key, storageArea: localStorage }));
}

beforeEach(() => {
  localStorage.clear();
  useAuthStore.setState({
    accessToken: null,
    refreshToken: null,
    user: null,
    isAuthenticated: false,
    lastSubject: null,
  });
  useAuthStore.getState().setTokens(ALICE, "RA");
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  stopReset = resetOnSignOut(client);
  cacheSizeAtSignOut = [];
  stopWatch = useAuthStore.subscribe((s, prev) => {
    if (prev.isAuthenticated && !s.isAuthenticated) {
      cacheSizeAtSignOut.push(client.getQueryCache().getAll().length);
    }
  });
  usePreferencesStore.setState({ savedFilters: { inventory: { search: "alice" } }, loaded: true });
  server.use(
    http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
    http.get("/api/reservations/:id", ({ request }) => {
      const token = (request.headers.get("Authorization") ?? "").replace("Bearer ", "");
      return tokenSubject(token) === "alice"
        ? HttpResponse.json({ title: "Alice's private reservation" })
        : HttpResponse.json({ detail: "Not found" }, { status: 404 });
    }),
    http.get("/api/thing", () => HttpResponse.json({ detail: "expired" }, { status: 401 })),
    http.post("/api/auth/refresh", () =>
      HttpResponse.json({ detail: "Invalid or expired refresh token" }, { status: 401 }),
    ),
    http.post("/api/auth/logout", () => new HttpResponse(null, { status: 204 })),
    http.post("/api/auth/login", () => HttpResponse.json({ access_token: BOB, refresh_token: "RB" })),
  );
});

afterEach(() => {
  stopWatch();
  stopReset();
  localStorage.clear();
});

async function expectAliceSignedOutCleanly() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument(), {
    timeout: 5000,
  });
  expect(cacheSizeAtSignOut).toEqual([0]);
  // Only the login page's own queries have been created since: config status
  // and the sign-in method (ADR 0016 phase 4), neither tied to an account.
  expect(client.getQueryCache().getAll().map((q) => q.queryKey[0]).sort()).toEqual([
    "configStatus",
    "loginOptions",
  ]);
  expect(usePreferencesStore.getState().savedFilters).toEqual({});
  expect(usePreferencesStore.getState().loaded).toBe(false);
}

async function bobSignsIn() {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "bob@x" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

async function expectBobOnTopologyWithoutAlicesData() {
  expect(await screen.findByText("At /topology")).toBeInTheDocument();
  expect(screen.queryByText("At /reservations/alice-res-1")).not.toBeInTheDocument();
  expect(screen.queryByText("Alice's private reservation")).not.toBeInTheDocument();
  expect(client.getQueryData(["reservation", "alice-res-1"])).toBeUndefined();
}

describe("every sign-out empties the cache and unbinds the return path (issue #1333 review)", () => {
  it("deliberate logout", async () => {
    renderApp();
    expect(await screen.findByText("Alice's private reservation")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    await expectAliceSignedOutCleanly();
    await bobSignsIn();
    await expectBobOnTopologyWithoutAlicesData();
  });

  it("another tab's logout arriving as a storage event", async () => {
    renderApp();
    expect(await screen.findByText("Alice's private reservation")).toBeInTheDocument();
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    storageEvent("access_token");
    await expectAliceSignedOutCleanly();
    await bobSignsIn();
    await expectBobOnTopologyWithoutAlicesData();
  });

  it("a refused refresh", { timeout: 10000 }, async () => {
    renderApp();
    expect(await screen.findByText("Alice's private reservation")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load" }));
    await expectAliceSignedOutCleanly();
    await bobSignsIn();
    await expectBobOnTopologyWithoutAlicesData();
  });

  it("another tab clearing the whole storage", async () => {
    renderApp();
    expect(await screen.findByText("Alice's private reservation")).toBeInTheDocument();
    localStorage.clear();
    storageEvent(null);
    await expectAliceSignedOutCleanly();
    await bobSignsIn();
    await expectBobOnTopologyWithoutAlicesData();
  });

  it("a switch to another account processed late (storage already holds bob's session)", async () => {
    renderApp();
    expect(await screen.findByText("Alice's private reservation")).toBeInTheDocument();
    const edges: number[] = [];
    const stop = useAuthStore.subscribe((s, prev) => {
      if (prev.isAuthenticated && !s.isAuthenticated) edges.push(1);
    });
    localStorage.setItem("refresh_token", "RB");
    localStorage.setItem("access_token", BOB);
    storageEvent("access_token");
    await expectBobOnTopologyWithoutAlicesData();
    stop();
    expect(edges).toEqual([1]);
    expect(cacheSizeAtSignOut).toEqual([0]);
    expect(useAuthStore.getState().accessToken).toBe(BOB);
    expect(usePreferencesStore.getState().savedFilters).toEqual({});
  });
});
