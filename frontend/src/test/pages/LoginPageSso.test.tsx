import { http, HttpResponse } from "msw";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, focusManager, onlineManager } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";
import { StrictMode, type ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastFn, toastError, navigateTopLevel } = vi.hoisted(() => {
  const toastFn = vi.fn();
  const toastError = vi.fn();
  return { toastFn, toastError, navigateTopLevel: vi.fn() };
});

vi.mock("react-hot-toast", () => ({
  default: Object.assign(toastFn, { error: toastError, success: vi.fn() }),
}));

vi.mock("@/lib/topLevelNavigation", () => ({ navigateTopLevel }));

import { server } from "../mocks/server";
import { flushPending } from "../flushPending";
import { fakeJwt } from "../fixtures/jwt";
import { LoginPage } from "@/pages/LoginPage";
import { GuestGuard } from "@/components/guards";
import { LOGIN_OPTIONS_TIMEOUT_MS } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

// ADR 0016 phase 4: the login page driven by GET /api/auth/login-options, its
// fallback to today's page (D10), the "Sign in with" button, the
// "Administrator sign-in" toggle, and the sso_error messages.

const LOCAL = { auth_method: "local", registration_open: true, password_login: true, sso: null };
const LDAP = { auth_method: "ldap", registration_open: false, password_login: true, sso: null };
const OIDC_OFF = {
  auth_method: "oidc",
  registration_open: false,
  password_login: false,
  sso: { provider_name: "Acme SSO" },
};
const OIDC_ON = { ...OIDC_OFF, password_login: true };

const FAILED = "Single sign-on failed. If this continues, contact an administrator.";

function Probe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  return (
    <div data-testid="probe">
      <span data-testid="where">{location.pathname + location.search + location.hash}</span>
      <span data-testid="nav-type">{navigationType}</span>
      <span data-testid="state">{JSON.stringify(location.state ?? null)}</span>
    </div>
  );
}

interface RenderOptions {
  initial?: string | { pathname: string; search?: string; hash?: string; state?: unknown };
  strict?: boolean;
  probe?: boolean;
  /** A client from an earlier render, to model a second visit in the same page load. */
  client?: QueryClient;
}

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderLogin({
  initial = "/login",
  strict = false,
  probe = false,
  client = newClient(),
}: RenderOptions = {}) {
  let tree: ReactNode = (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route
            path="/login"
            element={
              <>
                <div data-testid="page">
                  <LoginPage />
                </div>
                {probe && <Probe />}
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  if (strict) tree = <StrictMode>{tree}</StrictMode>;
  const view = render(tree);
  return { ...view, client, page: () => screen.getByTestId("page") };
}

function answerOptions(answer: () => Response | Promise<Response>) {
  server.use(http.get("/api/auth/login-options", answer));
}

async function settled(client: QueryClient, status: "success" | "error") {
  await waitFor(() => expect(client.getQueryState(["loginOptions"])?.status).toBe(status));
}

/**
 * The page as it renders once the request has failed: the failure notice
 * (issue #1420 review) above today's page (ADR 0016 D10). The failure cases
 * are compared to it as is, the local-mode answer without the notice. Since
 * issue #1420 it is no longer what renders while pending.
 */
async function failedHtml(): Promise<string> {
  answerOptions(() => HttpResponse.json({ detail: "Not Found" }, { status: 404 }));
  const { client, page, unmount } = renderLogin();
  await settled(client, "error");
  const html = page().innerHTML;
  unmount();
  return html;
}

/** `html` with the failure notice (the one role=alert element) removed. */
function withoutNotice(html: string): string {
  const holder = document.createElement("div");
  holder.innerHTML = html;
  const notices = holder.querySelectorAll("[role='alert']");
  expect(notices).toHaveLength(1);
  notices[0].remove();
  return holder.innerHTML;
}

const OPTIONS_FAILED = "Sign-in options could not be loaded.";

function notice() {
  return screen.queryByRole("alert");
}

function tryAgain() {
  return screen.queryByRole("button", { name: "Try again" });
}

/**
 * A login-options handler held until `release` is called with the answer, and
 * a promise that resolves once the request has reached it, so a test asserts
 * the pending state after the request is provably in flight, never by timing.
 */
function heldOptions() {
  let arrived: () => void = () => {};
  const reached = new Promise<void>((resolve) => {
    arrived = resolve;
  });
  let answer: (body: unknown) => void = () => {};
  const gate = new Promise<unknown>((resolve) => {
    answer = resolve;
  });
  let calls = 0;
  answerOptions(async () => {
    calls += 1;
    arrived();
    return HttpResponse.json((await gate) as Record<string, unknown>);
  });
  return {
    reached,
    calls: () => calls,
    release: (body: unknown) => act(async () => answer(body)),
  };
}

/**
 * Records whether an element matching `selector` was EVER inserted into the
 * document, even one a later commit removed again: every added node is
 * checked, not only what the document holds when the test looks.
 */
function everRendered(selector: string) {
  let seen = document.querySelector(selector) !== null;
  const check = (records: MutationRecord[]) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node instanceof Element && (node.matches(selector) || node.querySelector(selector))) {
          seen = true;
        }
      }
    }
  };
  const observer = new MutationObserver(check);
  observer.observe(document.body, { childList: true, subtree: true });
  return {
    seen: () => {
      check(observer.takeRecords());
      return seen;
    },
    stop: () => observer.disconnect(),
  };
}

beforeEach(() => {
  toastFn.mockClear();
  toastError.mockClear();
  navigateTopLevel.mockClear();
  localStorage.clear();
  useAuthStore.setState({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
  server.use(http.get("/api/config/status", () => HttpResponse.json({ configured: true })));
});

function controls() {
  return {
    sso: screen.queryByRole("button", { name: /^Sign in with / }),
    toggle: screen.queryByRole("button", { name: "Administrator sign-in" }),
    email: screen.queryByLabelText("Email"),
    password: screen.queryByLabelText("Password"),
    submit: screen.queryByRole("button", { name: "Sign in" }),
    register: screen.queryByRole("link", { name: "Register" }),
  };
}

interface ControlsShape {
  sso: boolean;
  toggle: boolean;
  form: boolean;
  register: boolean;
}

function controlsShape(present: Partial<ControlsShape>): ControlsShape {
  return { sso: false, toggle: false, form: false, register: false, ...present };
}

/** Which sign-in controls the page shows; the form counts only when all three of its controls are there. */
function presentControls(): ControlsShape {
  const c = controls();
  const parts = [c.email, c.password, c.submit].filter((el) => el !== null).length;
  if (parts !== 0 && parts !== 3) throw new Error(`a partial password form: ${parts} of 3 controls`);
  return { sso: c.sso !== null, toggle: c.toggle !== null, form: parts === 3, register: c.register !== null };
}

describe("LoginPage rendering is a pure function of /login-options (ADR 0016)", () => {
  it("local mode: today's form and the Register link, no single sign-on", async () => {
    answerOptions(() => HttpResponse.json(LOCAL));
    const { client } = renderLogin();
    await settled(client, "success");
    const c = controls();
    expect(c.sso).toBeNull();
    expect(c.toggle).toBeNull();
    expect(c.email).toBeInTheDocument();
    expect(c.password).toBeInTheDocument();
    expect(c.submit).toBeInTheDocument();
    expect(c.register).toHaveAttribute("href", "/register");
  });

  it("LDAP mode: today's form with no Register link (registration answers 409 there)", async () => {
    answerOptions(() => HttpResponse.json(LDAP));
    const { client } = renderLogin();
    await settled(client, "success");
    const c = controls();
    expect(c.sso).toBeNull();
    expect(c.toggle).toBeNull();
    expect(c.email).toBeInTheDocument();
    expect(c.password).toBeInTheDocument();
    expect(c.register).toBeNull();
    expect(screen.queryByText(/No account\?/)).toBeNull();
  });

  it("OIDC mode, break-glass off: only the Sign in with button", async () => {
    answerOptions(() => HttpResponse.json(OIDC_OFF));
    const { client } = renderLogin();
    await settled(client, "success");
    const c = controls();
    expect(c.sso).toHaveTextContent("Sign in with Acme SSO");
    expect(c.toggle).toBeNull();
    expect(c.email).toBeNull();
    expect(c.password).toBeNull();
    expect(c.submit).toBeNull();
    expect(c.register).toBeNull();
  });

  it("OIDC mode, break-glass on: the button, and the password form behind a collapsed toggle", async () => {
    answerOptions(() => HttpResponse.json(OIDC_ON));
    const { client } = renderLogin();
    await settled(client, "success");
    const c = controls();
    expect(c.sso).toHaveTextContent("Sign in with Acme SSO");
    expect(c.toggle).toHaveAttribute("aria-expanded", "false");
    expect(c.email).toBeNull();
    expect(c.password).toBeNull();
    expect(c.register).toBeNull();
  });

  const combos: Array<[boolean, boolean, boolean]> = [];
  for (const sso of [true, false]) {
    for (const pw of [true, false]) {
      for (const reg of [true, false]) combos.push([sso, pw, reg]);
    }
  }

  it.each(combos)(
    "sso present %s, password_login %s, registration_open %s",
    async (ssoPresent, passwordLogin, registrationOpen) => {
      answerOptions(() =>
        HttpResponse.json({
          auth_method: ssoPresent ? "oidc" : "local",
          password_login: passwordLogin,
          registration_open: registrationOpen,
          sso: ssoPresent ? { provider_name: "Corp" } : null,
        }),
      );
      const { client } = renderLogin();
      const valid = ssoPresent || passwordLogin;
      await settled(client, valid ? "success" : "error");
      const c = controls();
      if (!valid) {
        // No sign-in method: not an answer the backend gives, so today's page.
        expect(c.sso).toBeNull();
        expect(c.email).toBeInTheDocument();
        expect(c.register).toBeInTheDocument();
        return;
      }
      expect(c.sso !== null).toBe(ssoPresent);
      expect(c.toggle !== null).toBe(ssoPresent && passwordLogin);
      expect(c.email !== null).toBe(passwordLogin && !ssoPresent);
      expect(c.register !== null).toBe(registrationOpen);
    },
  );

  it("an sso object with a blank provider_name shows the default name", async () => {
    answerOptions(() =>
      HttpResponse.json({ ...OIDC_OFF, sso: { provider_name: "  " } }),
    );
    const { client } = renderLogin();
    await settled(client, "success");
    expect(controls().sso).toHaveTextContent("Sign in with single sign-on");
  });
});

describe("LoginPage while /login-options is pending (issue #1420)", () => {
  it("renders no sign-in method while the answer is pending", async () => {
    const held = heldOptions();
    const { page } = renderLogin();
    await held.reached;
    const c = controls();
    expect(c.sso).toBeNull();
    expect(c.toggle).toBeNull();
    expect(c.email).toBeNull();
    expect(c.password).toBeNull();
    expect(c.submit).toBeNull();
    expect(c.register).toBeNull();
    expect(screen.queryByText(/No account\?/)).toBeNull();
    expect(page().querySelector("form, input")).toBeNull();
  });

  it("announces the pending state as a polite status, with the placeholder bars hidden from assistive technology", async () => {
    const held = heldOptions();
    renderLogin();
    await held.reached;
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("Loading sign-in options...");
    const bars = status.querySelectorAll(".animate-pulse");
    expect(bars.length).toBeGreaterThan(0);
    for (const bar of bars) expect(status).toContainElement(bar.closest<HTMLElement>("[aria-hidden='true']"));
    await held.release(LOCAL);
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });

  it("keeps the header, the version, and the configuration link while pending", async () => {
    const held = heldOptions();
    renderLogin();
    await held.reached;
    expect(screen.getByRole("heading", { name: "HERD" })).toBeInTheDocument();
    expect(screen.getByTestId("app-version-login")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "System configuration" })).toHaveAttribute("href", "/config");
  });

  it("shows the unconfigured banner while pending: it depends on the config service, not on the method", async () => {
    server.use(http.get("/api/config/status", () => HttpResponse.json({ configured: false })));
    const held = heldOptions();
    renderLogin();
    await held.reached;
    await screen.findByText(/System requires initial configuration/);
    expect(controls().email).toBeNull();
    expect(controls().sso).toBeNull();
  });

  const MODES: Array<[string, Record<string, unknown>, ReturnType<typeof controlsShape>]> = [
    ["local", LOCAL, controlsShape({ form: true, register: true })],
    ["LDAP", LDAP, controlsShape({ form: true })],
    ["OIDC, break-glass off", OIDC_OFF, controlsShape({ sso: true })],
    ["OIDC, break-glass on", OIDC_ON, controlsShape({ sso: true, toggle: true })],
  ];
  it.each(MODES)("%s: nothing before the answer, then exactly what it states", async (_label, answer, shape) => {
    const held = heldOptions();
    const { client } = renderLogin();
    await held.reached;
    expect(presentControls()).toEqual(controlsShape({}));
    await held.release(answer);
    await settled(client, "success");
    expect(presentControls()).toEqual(shape);
    expect(screen.queryByRole("status")).toBeNull();
    // A confirmed answer, local mode's included, never shows the failure notice.
    expect(notice()).toBeNull();
    expect(tryAgain()).toBeNull();
    expect(held.calls()).toBe(1);
  });

  it.each([
    ["break-glass off", OIDC_OFF],
    ["break-glass on", OIDC_ON],
  ] as const)("OIDC mode, %s: the password form and the Register link are never in the document, from first render to the answer", async (_label, answer) => {
    const form = everRendered("#login-email, #login-password, button[type='submit']");
    const register = everRendered("a[href='/register']");
    try {
      const held = heldOptions();
      const { client } = renderLogin();
      await held.reached;
      await held.release(answer);
      await settled(client, "success");
      await screen.findByRole("button", { name: "Sign in with Acme SSO" });
      expect(form.seen()).toBe(false);
      expect(register.seen()).toBe(false);
    } finally {
      form.stop();
      register.stop();
    }
  });

  it("does not move focus when the answer arrives: a focused configuration link keeps it", async () => {
    const held = heldOptions();
    renderLogin();
    await held.reached;
    const user = userEvent.setup();
    await user.tab();
    const wrench = screen.getByRole("link", { name: "System configuration" });
    expect(wrench).toHaveFocus();
    await held.release(OIDC_ON);
    await screen.findByRole("button", { name: "Sign in with Acme SSO" });
    expect(wrench).toHaveFocus();
  });

  it("a request that never answers ends in the failure notice and today's page at the client timeout, not before", async () => {
    answerOptions(() => new Promise<never>(() => {}) as never);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      renderLogin();
      await act(() => vi.advanceTimersByTimeAsync(LOGIN_OPTIONS_TIMEOUT_MS - 1));
      expect(screen.getByRole("status")).toHaveTextContent("Loading sign-in options...");
      expect(controls().email).toBeNull();
      await act(() => vi.advanceTimersByTimeAsync(1));
      // TanStack notifies observers on a zero-delay timer, also fake here.
      await act(() => vi.advanceTimersByTimeAsync(10));
    } finally {
      vi.useRealTimers();
    }
    await screen.findByLabelText("Email");
    const c = controls();
    expect(c.password).toBeInTheDocument();
    expect(c.register).toBeInTheDocument();
    expect(c.sso).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(notice()).toHaveTextContent(OPTIONS_FAILED);
    expect(tryAgain()).toBeInTheDocument();
  });

  it("a signed-in visitor is sent on by GuestGuard before the page asks which method is active", async () => {
    let calls = 0;
    answerOptions(() => {
      calls += 1;
      return HttpResponse.json(LOCAL);
    });
    useAuthStore.getState().setTokens(fakeJwt("alice"), "R");
    render(
      <QueryClientProvider client={newClient()}>
        <MemoryRouter initialEntries={["/login"]}>
          <Routes>
            <Route
              path="/login"
              element={
                <GuestGuard>
                  <LoginPage />
                </GuestGuard>
              }
            />
            <Route path="/topology" element={<div data-testid="landed" />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByTestId("landed");
    await flushPending();
    expect(calls).toBe(0);
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("LoginPage after an answer or a failure (ADR 0016 D10, issue #1420)", () => {
  const OPTIONS_FAILURES: Array<[string, () => Response]> = [
    ["a 404 from a backend without phase 3", () => HttpResponse.json({ detail: "Not Found" }, { status: 404 })],
    ["a 500", () => HttpResponse.json({ detail: "boom" }, { status: 500 })],
    ["a 503", () => new HttpResponse("Service Unavailable", { status: 503 })],
    ["a network failure", () => HttpResponse.error()],
    [
      "malformed JSON",
      () => new HttpResponse("{oops", { status: 200, headers: { "Content-Type": "application/json" } }),
    ],
    ["an HTML 200", () => new HttpResponse("<!doctype html><p>hi</p>", { status: 200 })],
    ["a partial body", () => HttpResponse.json({ sso: { provider_name: "Acme" } })],
    ["ill-typed fields", () => HttpResponse.json({ ...OIDC_OFF, password_login: "false" })],
  ];
  it.each(OPTIONS_FAILURES)("on %s renders the failure notice above byte-for-byte today's page", async (_label, answer) => {
    const failed = await failedHtml();
    answerOptions(answer);
    const { client, page } = renderLogin();
    await settled(client, "error");
    expect(page().innerHTML).toBe(failed);
    expect(notice()).toHaveTextContent(OPTIONS_FAILED);
    expect(notice()).toContainElement(tryAgain());
    expect(presentControls()).toEqual(controlsShape({ form: true, register: true }));
    expect(screen.queryByText("boom")).toBeNull();
  });

  it("today's page is the password form and the Register link, with no single sign-on", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { client } = renderLogin();
    await settled(client, "error");
    expect(presentControls()).toEqual(controlsShape({ form: true, register: true }));
  });

  it("local mode's answer renders byte-for-byte today's page, without the failure notice", async () => {
    const failed = await failedHtml();
    answerOptions(() => HttpResponse.json(LOCAL));
    const { client, page } = renderLogin();
    await settled(client, "success");
    expect(page().innerHTML).toBe(withoutNotice(failed));
    expect(notice()).toBeNull();
  });

  it("LDAP mode's answer differs from today's page by the Register line alone", async () => {
    const failed = await failedHtml();
    answerOptions(() => HttpResponse.json(LDAP));
    const { client, page } = renderLogin();
    await settled(client, "success");
    const expected = document.createElement("div");
    expected.innerHTML = withoutNotice(failed);
    const registerLine = [...expected.querySelectorAll("p")].find((p) =>
      p.textContent?.startsWith("No account?"),
    );
    expect(registerLine).toBeDefined();
    registerLine!.remove();
    expect(page().innerHTML).toBe(expected.innerHTML);
  });

  it("today's page stays put under a user filling it in: no refetch on focus or on a network reconnect", async () => {
    let calls = 0;
    answerOptions(() => {
      calls += 1;
      return HttpResponse.json({ detail: "boom" }, { status: 500 });
    });
    const { client } = renderLogin();
    await settled(client, "error");
    const email = screen.getByLabelText("Email") as HTMLInputElement;
    await userEvent.type(email, "u@test");
    expect(email).toHaveFocus();
    try {
      act(() => {
        onlineManager.setOnline(false);
        onlineManager.setOnline(true);
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
      await flushPending();
    } finally {
      onlineManager.setOnline(true);
      focusManager.setFocused(undefined);
    }
    // A refetch would have put the query back to pending and removed the form.
    expect(calls).toBe(1);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByLabelText("Email")).toBe(email);
    expect(email).toHaveFocus();
    expect(email.value).toBe("u@test");
  });

  it("a second visit after a failure asks again, pending until the answer", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const first = renderLogin();
    await settled(first.client, "error");
    first.unmount();
    const held = heldOptions();
    renderLogin({ client: first.client });
    await held.reached;
    expect(presentControls()).toEqual(controlsShape({}));
    expect(screen.getByRole("status")).toHaveTextContent("Loading sign-in options...");
    await held.release(OIDC_OFF);
    await settled(first.client, "success");
    expect(presentControls()).toEqual(controlsShape({ sso: true }));
  });

  it("a confirmed answer stays up when a background refetch fails", async () => {
    answerOptions(() => HttpResponse.json(OIDC_OFF));
    const { client } = renderLogin();
    await settled(client, "success");
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    await act(async () => {
      await client.refetchQueries({ queryKey: ["loginOptions"] });
    });
    await settled(client, "error");
    expect(presentControls()).toEqual(controlsShape({ sso: true }));
    expect(notice()).toBeNull();
  });

  it("a second visit in the same page load renders the cached answer at once, with no pending state", async () => {
    answerOptions(() => HttpResponse.json(OIDC_OFF));
    const first = renderLogin();
    await settled(first.client, "success");
    first.unmount();
    const form = everRendered("#login-email, a[href='/register']");
    try {
      heldOptions();
      renderLogin({ client: first.client });
      expect(screen.getByRole("button", { name: "Sign in with Acme SSO" })).toBeInTheDocument();
      expect(screen.queryByRole("status")).toBeNull();
      await flushPending();
      expect(form.seen()).toBe(false);
    } finally {
      form.stop();
    }
  });
});

describe("the failure notice and Try again (issue #1420 review)", () => {
  const CAUSES: Array<[string, () => Response]> = [
    ["a 404", () => HttpResponse.json({ detail: "Not Found" }, { status: 404 })],
    ["a 5xx", () => HttpResponse.json({ detail: "boom" }, { status: 502 })],
    ["a network failure", () => HttpResponse.error()],
    ["an invalid body", () => HttpResponse.json({ password_login: "yes" })],
  ];
  it.each(CAUSES)("%s shows the notice and a Try again button above today's form", async (_label, answer) => {
    answerOptions(answer);
    const { client } = renderLogin();
    await settled(client, "error");
    expect(notice()).toHaveTextContent(OPTIONS_FAILED);
    expect(tryAgain()).toHaveAttribute("type", "button");
    expect(presentControls()).toEqual(controlsShape({ form: true, register: true }));
    // Above the form, in document order.
    expect(notice()!.compareDocumentPosition(screen.getByLabelText("Email"))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("a browser offline at load still sends the request; its failure shows the notice", async () => {
    let calls = 0;
    answerOptions(() => {
      calls += 1;
      return HttpResponse.error();
    });
    onlineManager.setOnline(false);
    try {
      const { client } = renderLogin();
      await settled(client, "error");
    } finally {
      onlineManager.setOnline(true);
    }
    expect(calls).toBe(1);
    expect(notice()).toHaveTextContent(OPTIONS_FAILED);
  });

  it("Try again goes back to pending, then renders the confirmed OIDC answer: form and Register gone, the button present", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { client } = renderLogin();
    await settled(client, "error");
    const held = heldOptions();
    await userEvent.click(tryAgain()!);
    await held.reached;
    await flushPending();
    expect(screen.getByRole("status")).toHaveTextContent("Loading sign-in options...");
    expect(presentControls()).toEqual(controlsShape({}));
    expect(notice()).toBeNull();
    await held.release(OIDC_OFF);
    await settled(client, "success");
    expect(presentControls()).toEqual(controlsShape({ sso: true }));
    expect(notice()).toBeNull();
  });

  it("Try again that fails again returns to today's page with the notice", async () => {
    let calls = 0;
    answerOptions(() => {
      calls += 1;
      return HttpResponse.json({ detail: "boom" }, { status: 500 });
    });
    const { client } = renderLogin();
    await settled(client, "error");
    await userEvent.click(tryAgain()!);
    await waitFor(() => expect(calls).toBe(2));
    await settled(client, "error");
    await screen.findByRole("alert");
    expect(notice()).toHaveTextContent(OPTIONS_FAILED);
    expect(presentControls()).toEqual(controlsShape({ form: true, register: true }));
  });

  it("repeated clicks on Try again send one request", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { client } = renderLogin();
    await settled(client, "error");
    const held = heldOptions();
    const button = tryAgain()!;
    fireEvent.click(button);
    // Still in the document: the page re-renders only on TanStack's next tick.
    expect(button.isConnected).toBe(true);
    fireEvent.click(button);
    fireEvent.click(button);
    await held.reached;
    await flushPending();
    expect(held.calls()).toBe(1);
    await held.release(LOCAL);
    await settled(client, "success");
    expect(held.calls()).toBe(1);
  });

  it("each retry gets the same client limit: a retry that never answers ends in the notice again", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { client } = renderLogin();
    await settled(client, "error");
    answerOptions(() => new Promise<never>(() => {}) as never);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(tryAgain()!);
      await act(() => vi.advanceTimersByTimeAsync(LOGIN_OPTIONS_TIMEOUT_MS - 1));
      expect(screen.getByRole("status")).toHaveTextContent("Loading sign-in options...");
      expect(notice()).toBeNull();
      await act(() => vi.advanceTimersByTimeAsync(1));
      await act(() => vi.advanceTimersByTimeAsync(10));
    } finally {
      vi.useRealTimers();
    }
    expect(await screen.findByRole("alert")).toHaveTextContent(OPTIONS_FAILED);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("text typed into today's form survives a retry that fails again", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { client } = renderLogin();
    await settled(client, "error");
    await userEvent.type(screen.getByLabelText("Email"), "u@test");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    const held = heldOptions();
    await userEvent.click(tryAgain()!);
    await held.reached;
    await flushPending();
    expect(screen.queryByLabelText("Email")).toBeNull();
    await act(async () => {
      held.release(null);
    });
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Email")).toHaveValue("u@test");
    expect(screen.getByLabelText("Password")).toHaveValue("pw");
  });

  it("a retry that confirms local mode keeps the email and the password", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { client } = renderLogin();
    await settled(client, "error");
    await userEvent.type(screen.getByLabelText("Email"), "u@test");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    answerOptions(() => HttpResponse.json(LOCAL));
    await userEvent.click(tryAgain()!);
    await settled(client, "success");
    await waitFor(() => expect(notice()).toBeNull());
    expect(screen.getByLabelText("Email")).toHaveValue("u@test");
    expect(screen.getByLabelText("Password")).toHaveValue("pw");
  });

  it("a retry that confirms single sign-on keeps the email but drops the password behind the toggle", async () => {
    answerOptions(() => HttpResponse.json({ detail: "boom" }, { status: 500 }));
    const { client } = renderLogin();
    await settled(client, "error");
    await userEvent.type(screen.getByLabelText("Email"), "root@test");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    answerOptions(() => HttpResponse.json(OIDC_ON));
    await userEvent.click(tryAgain()!);
    await settled(client, "success");
    await userEvent.click(await screen.findByRole("button", { name: "Administrator sign-in" }));
    expect(screen.getByLabelText("Email")).toHaveValue("root@test");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });
});

describe("the Sign in with button (ADR 0016)", () => {
  it("starts the flow with one top-level navigation to the fixed begin route, never an XHR", async () => {
    let xhrBegins = 0;
    server.use(
      http.get("/api/auth/oidc/login", () => {
        xhrBegins += 1;
        return new HttpResponse(null, { status: 302 });
      }),
    );
    answerOptions(() => HttpResponse.json(OIDC_OFF));
    const { client } = renderLogin({
      initial: { pathname: "/login", search: "?next=//evil.example", state: { from: "/inventory", sub: null } },
    });
    await settled(client, "success");
    const button = screen.getByRole("button", { name: "Sign in with Acme SSO" });
    expect(button).toHaveAttribute("type", "button");
    await userEvent.click(button);
    expect(navigateTopLevel).toHaveBeenCalledTimes(1);
    // Exactly the begin route: no next, nothing from the page's URL or state.
    expect(navigateTopLevel).toHaveBeenCalledWith("/api/auth/oidc/login");
    expect(xhrBegins).toBe(0);
  });

  it("is keyboard operable", async () => {
    answerOptions(() => HttpResponse.json(OIDC_OFF));
    const { client } = renderLogin();
    await settled(client, "success");
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole("button", { name: "Sign in with Acme SSO" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(navigateTopLevel).toHaveBeenCalledWith("/api/auth/oidc/login");
  });

  it("is disabled while the system is unconfigured, like the form", async () => {
    server.use(http.get("/api/config/status", () => HttpResponse.json({ configured: false })));
    answerOptions(() => HttpResponse.json(OIDC_OFF));
    const { client } = renderLogin();
    await settled(client, "success");
    await screen.findByText(/System requires initial configuration/);
    const button = screen.getByRole("button", { name: "Sign in with Acme SSO" });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(navigateTopLevel).not.toHaveBeenCalled();
  });

  it("renders the provider name as text, never as markup", async () => {
    answerOptions(() =>
      HttpResponse.json({ ...OIDC_OFF, sso: { provider_name: "<img src=x onerror=alert(1)>" } }),
    );
    const { client, page } = renderLogin();
    await settled(client, "success");
    expect(screen.getByRole("button", { name: "Sign in with <img src=x onerror=alert(1)>" })).toBeInTheDocument();
    expect(page().querySelector("img")).toBeNull();
  });
});

describe("the Administrator sign-in toggle (ADR 0016)", () => {
  it("is a collapsed disclosure that reveals the password form it controls", async () => {
    answerOptions(() => HttpResponse.json(OIDC_ON));
    const { client } = renderLogin();
    await settled(client, "success");
    const toggle = screen.getByRole("button", { name: "Administrator sign-in" });
    expect(toggle).toHaveAttribute("type", "button");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const panel = document.getElementById(toggle.getAttribute("aria-controls")!);
    expect(panel).not.toBeNull();
    expect(screen.queryByLabelText("Email")).toBeNull();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const email = screen.getByLabelText("Email");
    expect(panel).toContainElement(email);
    expect(panel).toContainElement(screen.getByLabelText("Password"));
    expect((screen.getByLabelText("Password") as HTMLInputElement).placeholder).toBe("password");
    expect(panel).toContainElement(screen.getByRole("button", { name: "Sign in" }));

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Email")).toBeNull();
  });

  it("opens and closes from the keyboard, keeps focus on itself, and the form follows it in tab order", async () => {
    answerOptions(() => HttpResponse.json(OIDC_ON));
    const { client } = renderLogin();
    await settled(client, "success");
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole("button", { name: "Sign in with Acme SSO" })).toHaveFocus();
    await user.tab();
    const toggle = screen.getByRole("button", { name: "Administrator sign-in" });
    expect(toggle).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveFocus();
    await user.tab();
    expect(screen.getByLabelText("Email")).toHaveFocus();
    await user.tab({ shift: true });
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps one primary action: the form's submit is secondary behind the toggle", async () => {
    answerOptions(() => HttpResponse.json(OIDC_ON));
    const { client, page } = renderLogin();
    await settled(client, "success");
    await userEvent.click(screen.getByRole("button", { name: "Administrator sign-in" }));
    const submit = screen.getByRole("button", { name: "Sign in" });
    expect(submit).toHaveClass("bg-gray-100");
    expect(submit).not.toHaveClass("bg-blue-600");
    const primaries = [...page().querySelectorAll("button")].filter((b) => b.classList.contains("bg-blue-600"));
    expect(primaries).toEqual([screen.getByRole("button", { name: "Sign in with Acme SSO" })]);
  });

  it("leaves the submit primary wherever the form is not behind the toggle", async () => {
    for (const options of [LOCAL, LDAP]) {
      answerOptions(() => HttpResponse.json(options));
      const { client, unmount } = renderLogin();
      await settled(client, "success");
      expect(screen.getByRole("button", { name: "Sign in" })).toHaveClass("bg-blue-600");
      unmount();
    }
  });

  it("the form behind it signs in exactly like today's form", async () => {
    let body: unknown = null;
    server.use(
      http.post("/api/auth/login", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ access_token: "A", refresh_token: "R" });
      }),
    );
    answerOptions(() => HttpResponse.json(OIDC_ON));
    const { client } = renderLogin({ probe: true });
    await settled(client, "success");
    await userEvent.click(screen.getByRole("button", { name: "Administrator sign-in" }));
    await userEvent.type(screen.getByLabelText("Email"), "root@test");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(body).toEqual({ email: "root@test", password: "pw" }));
    await waitFor(() => expect(useAuthStore.getState().accessToken).toBe("A"));
  });
});

describe("sso_error on the login page (ADR 0016)", () => {
  it.each([
    ["cancelled", "Sign-in was cancelled.", "neutral"],
    ["unavailable", "Single sign-on is unavailable right now. Try again later.", "error"],
    ["failed", FAILED, "error"],
  ] as const)("%s shows its fixed message once", async (value, message, kind) => {
    answerOptions(() => HttpResponse.json(LOCAL));
    renderLogin({ initial: `/login?sso_error=${value}`, probe: true });
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/login$/));
    const shown = kind === "neutral" ? toastFn : toastError;
    const other = kind === "neutral" ? toastError : toastFn;
    expect(shown).toHaveBeenCalledTimes(1);
    expect(shown).toHaveBeenCalledWith(message, { id: "sso-error" });
    expect(other).not.toHaveBeenCalled();
  });

  it.each(["", "access_denied", "CANCELLED", "%3Cscript%3Ealert(1)%3C%2Fscript%3E", "Call+555-0100"])(
    "any other value (%j) shows the failed message and is never reflected into the page",
    async (raw) => {
      answerOptions(() => HttpResponse.json(LOCAL));
      renderLogin({ initial: `/login?sso_error=${raw}`, probe: true });
      await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/login$/));
      expect(toastError).toHaveBeenCalledTimes(1);
      expect(toastError).toHaveBeenCalledWith(FAILED, { id: "sso-error" });
      const decoded = decodeURIComponent(raw.replace(/\+/g, " "));
      if (decoded) expect(screen.getByTestId("page").innerHTML).not.toContain(decoded);
      expect(document.querySelector("script")).toBeNull();
    },
  );

  it("removes only that parameter, with a replace, keeping the rest of the URL and the return-path state", async () => {
    answerOptions(() => HttpResponse.json(LOCAL));
    const state = { from: "/inventory", sub: null };
    renderLogin({
      initial: { pathname: "/login", search: "?a=1&sso_error=failed&b=2", hash: "#h", state },
      probe: true,
    });
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/login?a=1&b=2#h"));
    expect(screen.getByTestId("nav-type")).toHaveTextContent("REPLACE");
    expect(screen.getByTestId("state")).toHaveTextContent(JSON.stringify(state));
  });

  it("does nothing without the parameter: no toast, no navigation", async () => {
    answerOptions(() => HttpResponse.json(LOCAL));
    const { client } = renderLogin({ initial: "/login?a=1", probe: true });
    await settled(client, "success");
    expect(toastFn).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
    expect(screen.getByTestId("where")).toHaveTextContent("/login?a=1");
    expect(screen.getByTestId("nav-type")).toHaveTextContent("POP");
  });

  it("toasts once under StrictMode's double effect", async () => {
    answerOptions(() => HttpResponse.json(LOCAL));
    renderLogin({ initial: "/login?sso_error=unavailable", probe: true, strict: true });
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(/^\/login$/));
    await act(async () => {});
    expect(toastError).toHaveBeenCalledTimes(1);
  });

  it("shows the message even when /login-options fails", async () => {
    answerOptions(() => HttpResponse.json({ detail: "Not Found" }, { status: 404 }));
    renderLogin({ initial: "/login?sso_error=failed", probe: true });
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(FAILED, { id: "sso-error" }));
  });
});
