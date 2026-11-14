import { http, HttpResponse } from "msw";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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
import { LoginPage } from "@/pages/LoginPage";
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
}

function renderLogin({ initial = "/login", strict = false, probe = false }: RenderOptions = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
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

/** The page as it renders before any answer: today's page. */
function legacyHtml(): string {
  answerOptions(() => new Promise<never>(() => {}) as never);
  const { page, unmount } = renderLogin();
  const html = page().innerHTML;
  unmount();
  return html;
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

describe("LoginPage fallback to today's page (ADR 0016 D10)", () => {
  it("renders today's page while the answer is pending", () => {
    answerOptions(() => new Promise<never>(() => {}) as never);
    renderLogin();
    const c = controls();
    expect(c.sso).toBeNull();
    expect(c.email).toBeInTheDocument();
    expect(c.password).toBeInTheDocument();
    expect(c.register).toBeInTheDocument();
  });

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
  it.each(OPTIONS_FAILURES)("on %s renders byte-for-byte today's page", async (_label, answer) => {
    const legacy = legacyHtml();
    answerOptions(answer);
    const { client, page } = renderLogin();
    await settled(client, "error");
    expect(page().innerHTML).toBe(legacy);
    expect(screen.queryByText("boom")).toBeNull();
  });

  it("local mode's answer renders byte-for-byte today's page", async () => {
    const legacy = legacyHtml();
    answerOptions(() => HttpResponse.json(LOCAL));
    const { client, page } = renderLogin();
    await settled(client, "success");
    expect(page().innerHTML).toBe(legacy);
  });

  it("LDAP mode's answer differs from today's page by the Register line alone", async () => {
    const legacy = legacyHtml();
    answerOptions(() => HttpResponse.json(LDAP));
    const { client, page } = renderLogin();
    await settled(client, "success");
    const expected = document.createElement("div");
    expected.innerHTML = legacy;
    const registerLine = [...expected.querySelectorAll("p")].find((p) =>
      p.textContent?.startsWith("No account?"),
    );
    expect(registerLine).toBeDefined();
    registerLine!.remove();
    expect(page().innerHTML).toBe(expected.innerHTML);
  });

  it("a local-mode answer keeps the same input elements, so a filled-in form survives it", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    answerOptions(async () => {
      await gate;
      return HttpResponse.json(LOCAL);
    });
    const { client } = renderLogin();
    const email = screen.getByLabelText("Email") as HTMLInputElement;
    const password = screen.getByLabelText("Password") as HTMLInputElement;
    await userEvent.type(email, "u@test");
    release();
    await settled(client, "success");
    expect(screen.getByLabelText("Email")).toBe(email);
    expect(screen.getByLabelText("Password")).toBe(password);
    expect(email.value).toBe("u@test");
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
