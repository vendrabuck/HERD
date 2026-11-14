import { http, HttpResponse } from "msw";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useNavigationType,
} from "react-router-dom";
import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { toastFn, toastError } = vi.hoisted(() => ({ toastFn: vi.fn(), toastError: vi.fn() }));

vi.mock("react-hot-toast", () => ({
  default: Object.assign(toastFn, { error: toastError, success: vi.fn() }),
}));

import { server } from "../mocks/server";
import { AuthGuard, GuestGuard } from "@/components/guards";
import { LoginPage } from "@/pages/LoginPage";
import { OidcCompletePage } from "@/pages/OidcCompletePage";
import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, useAuthStore } from "@/stores/authStore";
import { fakeJwt } from "../fixtures/jwt";

// ADR 0016 phase 4: /login/sso trades the handoff cookie for the session,
// through the real guards and the real login page.

const ACCESS = fakeJwt("sso-user-1");
const REFRESH = "REFRESH-SECRET-VALUE";
const FAILED = "Single sign-on failed. If this continues, contact an administrator.";

// Every location the app passes through, in order, and the location state of
// each /login/sso entry.
let visited: string[] = [];
let ssoStates: unknown[] = [];
// The router's navigate, so a test can move the app the way a click would.
let routerNavigate: ReturnType<typeof useNavigate> | null = null;

function Where() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const navigate = useNavigate();
  return (
    <div>
      <p data-testid="where">
        {location.pathname + location.search + location.hash} {navigationType}
      </p>
      <button type="button" onClick={() => navigate(-1)}>
        Back
      </button>
    </div>
  );
}

function Recorder() {
  const location = useLocation();
  routerNavigate = useNavigate();
  const entry = location.pathname + location.search + location.hash;
  if (visited[visited.length - 1] !== entry) visited.push(entry);
  if (location.pathname === "/login/sso" && location.state) ssoStates.push(location.state);
  return null;
}

function renderAt(initial: string | string[], { strict = false }: { strict?: boolean } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const tree = (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={Array.isArray(initial) ? initial : [initial]}>
        <Recorder />
        <Routes>
          <Route
            path="/login"
            element={
              <GuestGuard>
                <LoginPage />
                <Where />
              </GuestGuard>
            }
          />
          <Route
            path="/login/sso"
            element={
              <GuestGuard>
                <OidcCompletePage />
              </GuestGuard>
            }
          />
          <Route path="/before" element={<Where />} />
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
    </QueryClientProvider>
  );
  const view = render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  return { ...view, client, tree };
}

let exchangeCalls: Array<{ body: string; credentials: RequestCredentials; auth: string | null }> = [];
let refreshCalls = 0;
let consoleSpies: Array<ReturnType<typeof vi.spyOn>> = [];

function answerExchange(answer: () => Response | Promise<Response>) {
  server.use(
    http.post("/api/auth/oidc/exchange", async ({ request }) => {
      exchangeCalls.push({
        body: await request.text(),
        credentials: request.credentials,
        auth: request.headers.get("Authorization"),
      });
      return answer();
    }),
  );
}

function success(next: unknown = "/inventory?tab=x#y") {
  return () =>
    HttpResponse.json({ access_token: ACCESS, refresh_token: REFRESH, token_type: "bearer", next });
}

function consoleText(): string {
  return consoleSpies
    .flatMap((spy) => spy.mock.calls)
    .map((args) => args.map((a: unknown) => String(a)).join(" "))
    .join("\n");
}

beforeEach(() => {
  visited = [];
  ssoStates = [];
  routerNavigate = null;
  exchangeCalls = [];
  refreshCalls = 0;
  toastFn.mockClear();
  toastError.mockClear();
  localStorage.clear();
  useAuthStore.setState({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
  consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((name) =>
    vi.spyOn(console, name).mockImplementation(() => {}),
  );
  server.use(
    http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
    http.get("/api/auth/login-options", () =>
      HttpResponse.json({ auth_method: "local", registration_open: true, password_login: true, sso: null }),
    ),
    http.get("/api/auth/me", () => HttpResponse.json({ detail: "x" }, { status: 401 })),
    http.post("/api/auth/refresh", () => {
      refreshCalls += 1;
      return HttpResponse.json({ access_token: "n", refresh_token: "n" });
    }),
  );
});

afterEach(() => {
  consoleSpies.forEach((spy) => spy.mockRestore());
});

describe("OidcCompletePage (ADR 0016 phase 4)", () => {
  it("shows a status line while the exchange runs", () => {
    answerExchange(() => new Promise<never>(() => {}));
    renderAt("/login/sso");
    expect(screen.getByRole("status")).toHaveTextContent("Signing in...");
  });

  it("posts {} once with credentials, stores the pair like a password login, and replaces itself with next", async () => {
    answerExchange(success());
    renderAt("/login/sso");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/inventory?tab=x#y REPLACE"));
    expect(exchangeCalls).toHaveLength(1);
    expect(exchangeCalls[0].body).toBe("{}");
    expect(exchangeCalls[0].credentials).toBe("include");
    expect(exchangeCalls[0].auth).toBeNull();
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBe(ACCESS);
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe(REFRESH);
    expect(useAuthStore.getState()).toMatchObject({
      accessToken: ACCESS,
      refreshToken: REFRESH,
      isAuthenticated: true,
      lastSubject: "sso-user-1",
    });
    // GuestGuard's own redirect (to /topology) never won over next.
    expect(visited).toEqual(["/login/sso", "/inventory?tab=x#y"]);
  });

  it("refetches the signed-in user, as a password login does", async () => {
    answerExchange(success("/topology"));
    const { client } = renderAt("/login/sso");
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/topology"));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["currentUser"] });
  });

  it("spends the handoff exactly once under StrictMode's double effect and on re-render", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    answerExchange(async () => {
      await gate;
      return success()();
    });
    const { rerender, tree } = renderAt("/login/sso", { strict: true });
    await waitFor(() => expect(exchangeCalls).toHaveLength(1));
    rerender(<StrictMode>{tree}</StrictMode>);
    rerender(<StrictMode>{tree}</StrictMode>);
    await act(async () => {});
    expect(exchangeCalls).toHaveLength(1);
    release();
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/inventory?tab=x#y"));
    expect(exchangeCalls).toHaveLength(1);
  });

  it.each([
    "https://evil.example/topology",
    "//evil.example",
    "//evil.example/inventory",
    "/\\evil.example",
    "\\\\evil.example",
    "javascript:alert(document.cookie)",
    "data:text/html,x",
    "/inventory\n//evil.example",
    "inventory",
    "",
    "/login",
    "/login/sso",
    "/register",
    null,
    42,
  ])("navigates only within the app: next %j lands on /topology", async (next) => {
    answerExchange(success(next));
    renderAt("/login/sso");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/topology REPLACE$/));
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(visited.every((entry) => entry.startsWith("/") && !entry.startsWith("//"))).toBe(true);
  });

  const EXCHANGE_FAILURES: Array<[string, () => Response]> = [
    ["the 401", () => HttpResponse.json({ detail: "Single sign-on session expired; sign in again" }, { status: 401 })],
    ["a 404 outside OIDC mode", () => HttpResponse.json({ detail: "Single sign-on is not enabled" }, { status: 404 })],
    ["a 422", () => HttpResponse.json({ detail: [{ msg: "Extra inputs are not permitted" }] }, { status: 422 })],
    ["a 500", () => HttpResponse.json({ detail: "boom" }, { status: 500 })],
    ["a network error", () => HttpResponse.error()],
    ["a 200 without tokens", () => HttpResponse.json({ next: "/inventory" })],
    ["a 200 with one token", () => HttpResponse.json({ access_token: ACCESS, next: "/inventory" })],
    ["a 200 that is not JSON", () => new HttpResponse("<html></html>", { status: 200 })],
  ];
  it.each(EXCHANGE_FAILURES)("on %s stores nothing and shows the failed message on the login page", async (_label, answer) => {
    answerExchange(answer);
    renderAt("/login/sso");
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(FAILED, { id: "sso-error" }));
    // The login page read the parameter and replaced it away.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/login REPLACE$/));
    expect(visited).toEqual(["/login/sso", "/login?sso_error=failed", "/login"]);
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(exchangeCalls).toHaveLength(1);
    expect(refreshCalls).toBe(0);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    for (const raw of ["Single sign-on session expired", "not enabled", "boom", "Extra inputs"]) {
      expect(document.body.textContent).not.toContain(raw);
      expect(toastError.mock.calls.flat().join(" ")).not.toContain(raw);
    }
  });

  it("an exchange that never answers is given up after 15 seconds as a failure", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      answerExchange(() => new Promise<never>(() => {}));
      renderAt("/login/sso");
      await vi.advanceTimersByTimeAsync(14_999);
      expect(toastError).not.toHaveBeenCalled();
      expect(visited).toEqual(["/login/sso"]);
      await vi.advanceTimersByTimeAsync(1);
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(FAILED, { id: "sso-error" }));
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("writes no token to a URL or the console, on success or failure", async () => {
    answerExchange(success("/inventory"));
    const first = renderAt("/login/sso");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/inventory"));
    first.unmount();
    localStorage.clear();
    useAuthStore.setState({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
    answerExchange(() => HttpResponse.json({ detail: "Single sign-on session expired; sign in again" }, { status: 401 }));
    renderAt("/login/sso");
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    for (const entry of visited) {
      expect(entry).not.toContain(ACCESS);
      expect(entry).not.toContain(REFRESH);
      expect(entry).not.toMatch(/token|handoff/i);
    }
    const logged = consoleText();
    expect(logged).not.toContain(ACCESS);
    expect(logged).not.toContain(REFRESH);
    expect(logged).not.toContain("Single sign-on session expired");
  });

  it("a visitor already signed in is sent on by GuestGuard and spends no handoff", async () => {
    useAuthStore.getState().setTokens(fakeJwt("someone"), "R");
    answerExchange(success());
    renderAt("/login/sso");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/topology"));
    await act(async () => {});
    expect(exchangeCalls).toHaveLength(0);
  });

  it("records the return path bound to the exchanged token's account, never to any account", async () => {
    answerExchange(success("/inventory"));
    renderAt("/login/sso");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/inventory"));
    expect(ssoStates).toContainEqual({ from: "/inventory", sub: "sso-user-1" });
    expect(ssoStates.every((state) => (state as { sub?: unknown }).sub === "sso-user-1")).toBe(true);
  });

  it("an access token whose account cannot be read is a failure that stores nothing", async () => {
    answerExchange(() =>
      HttpResponse.json({ access_token: "not-a-jwt", refresh_token: REFRESH, next: "/inventory" }),
    );
    renderAt("/login/sso");
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(FAILED, { id: "sso-error" }));
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });

  it.each(["success", "failure"] as const)(
    "the review's race: another tab signs in while the exchange is in flight; its late %s changes nothing",
    async (outcome) => {
      let release: () => void = () => {};
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      answerExchange(async () => {
        await gate;
        return outcome === "success"
          ? success("/inventory/sso-only-page")()
          : HttpResponse.json({ detail: "expired" }, { status: 401 });
      });
      renderAt("/login/sso");
      await waitFor(() => expect(exchangeCalls).toHaveLength(1));

      // Another tab signs in as a different account; this tab adopts it.
      const other = fakeJwt("other-account");
      localStorage.setItem(REFRESH_TOKEN_KEY, "OTHER-REFRESH");
      localStorage.setItem(ACCESS_TOKEN_KEY, other);
      await act(async () => {
        window.dispatchEvent(new StorageEvent("storage", { key: ACCESS_TOKEN_KEY }));
      });
      await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/topology REPLACE"));

      release();
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(visited).toEqual(["/login/sso", "/topology"]);
      expect(screen.getByTestId("where")).toHaveTextContent("/topology REPLACE");
      expect(useAuthStore.getState().accessToken).toBe(other);
      expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBe(other);
      expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe("OTHER-REFRESH");
      expect(toastError).not.toHaveBeenCalled();
      expect(toastFn).not.toHaveBeenCalled();
      expect(exchangeCalls).toHaveLength(1);
    },
  );

  it.each(["success", "failure"] as const)(
    "after the page unmounts, a late %s causes no navigation, no token adoption, and no toast",
    async (outcome) => {
      let release: () => void = () => {};
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      answerExchange(async () => {
        await gate;
        return outcome === "success"
          ? success("/inventory")()
          : HttpResponse.json({ detail: "expired" }, { status: 401 });
      });
      renderAt("/login/sso");
      await waitFor(() => expect(exchangeCalls).toHaveLength(1));
      // The app moves on (a link, Back) while the router stays mounted.
      await act(async () => {
        routerNavigate!("/before");
      });
      await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/before PUSH$/));
      release();
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(visited).toEqual(["/login/sso", "/before"]);
      expect(screen.getByTestId("where")).toHaveTextContent(/^\/before PUSH$/);
      expect(ssoStates).toEqual([]);
      expect(useAuthStore.getState().isAuthenticated).toBe(false);
      expect(localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
      expect(toastError).not.toHaveBeenCalled();
      expect(toastFn).not.toHaveBeenCalled();
    },
  );

  it("a return path the flow recorded for one account is not honored for another", () => {
    useAuthStore.getState().setTokens(fakeJwt("other-account"), "R");
    render(
      <MemoryRouter
        initialEntries={[{ pathname: "/login/sso", state: { from: "/inventory/x", sub: "sso-user-1" } }]}
      >
        <Recorder />
        <Routes>
          <Route
            path="/login/sso"
            element={
              <GuestGuard>
                <p>sso page</p>
              </GuestGuard>
            }
          />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("where")).toHaveTextContent("/topology REPLACE");
  });

  it.each([
    ["success", "/inventory"],
    ["failure", "/login"],
  ] as const)(
    "on %s the page replaces itself: Back lands on the page before it and never re-posts",
    async (outcome, landing) => {
      answerExchange(
        outcome === "success"
          ? success("/inventory")
          : () => HttpResponse.json({ detail: "expired" }, { status: 401 }),
      );
      renderAt(["/before", "/login/sso"]);
      await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(new RegExp(`^${landing} REPLACE$`)));
      await act(async () => {
        screen.getByRole("button", { name: "Back" }).click();
      });
      await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/before POP$/));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(exchangeCalls).toHaveLength(1);
      expect(visited[visited.length - 1]).toBe("/before");
    },
  );
});
