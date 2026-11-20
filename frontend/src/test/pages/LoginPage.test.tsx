import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { mockNavigate, toastError, toastSuccess } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

import { server } from "../mocks/server";
import { LoginPage } from "@/pages/LoginPage";
import { APP_VERSION } from "@/lib/appVersion";
import { fakeJwt } from "../fixtures/jwt";
import {
  expectAuthorCredit,
  expectCreditStackedUnderCard,
  tabToAuthorCredit,
} from "../authorCredit";

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockNavigate.mockClear();
  toastError.mockClear();
  toastSuccess.mockClear();
  // A local-mode answer: the form renders once it arrives (issue #1420: no
  // sign-in method renders while it is pending), so each test finds the form
  // with a findBy query rather than assuming it is there at first render.
  server.use(
    http.get("/api/auth/login-options", () =>
      HttpResponse.json({ auth_method: "local", registration_open: true, password_login: true, sso: null }),
    ),
  );
});

describe("LoginPage", () => {
  it("renders the identifier, password, and submit controls", async () => {
    renderWithProviders(<LoginPage />);
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    const pw = screen.getByLabelText("Password") as HTMLInputElement;
    expect(pw).toBeInTheDocument();
    expect(pw.placeholder).toBe("password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows the injected app version, and nothing else version-shaped (issue #846)", () => {
    renderWithProviders(<LoginPage />);
    const version = screen.getByTestId("app-version-login");
    expect(version).toHaveTextContent(`v${APP_VERSION}`);
    // Anonymous visitors see the version only, never a build string or date.
    expect(version.textContent).toBe(`v${APP_VERSION}`);
  });

  it("shows the unconfigured banner and disables fields when configured=false", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: false }),
      ),
    );
    renderWithProviders(<LoginPage />);
    await waitFor(() =>
      expect(
        screen.getByText(/System requires initial configuration/i),
      ).toBeInTheDocument(),
    );
    expect(await screen.findByLabelText("Email")).toBeDisabled();
    expect(screen.getByLabelText("Password")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("navigates to /topology on successful login", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true }),
      ),
      http.post("/api/auth/login", () =>
        HttpResponse.json({
          access_token: "a",
          refresh_token: "r",
          user: { id: "u1", email: "u@test", username: "u", role: "user" },
        }),
      ),
    );
    renderWithProviders(<LoginPage />);
    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/topology", { replace: true }),
    );
  });

  it("toasts an error when login is rejected", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true }),
      ),
      http.post("/api/auth/login", () =>
        HttpResponse.json({ detail: "bad creds" }, { status: 401 }),
      ),
    );
    renderWithProviders(<LoginPage />);
    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Invalid credentials. Check your email or login name and your password.",
      ),
    );
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

// Issue #1335: in LDAP mode the identifier is the directory login name
// (IAM-LDAP-2), which need not contain an @, and the page cannot yet tell the
// mode, so the field must accept any text in both modes.
describe("LoginPage identifier field (issue #1335)", () => {
  it("is a text field with username autocomplete and an email keyboard, never type=email", async () => {
    renderWithProviders(<LoginPage />);
    const id = (await screen.findByRole("textbox", { name: "Email" })) as HTMLInputElement;
    expect(id.type).toBe("text");
    expect(id.getAttribute("autocomplete")).toBe("username");
    expect(id.getAttribute("inputmode")).toBe("email");
    expect(id.placeholder).toBe("you@example.com");
    expect(id).toHaveAccessibleDescription("Directory accounts: enter your directory login name.");
    expect(id.id).toBe("login-email");
    const pw = screen.getByLabelText("Password") as HTMLInputElement;
    expect(pw.getAttribute("autocomplete")).toBe("current-password");
    expect(pw.placeholder).toBe("password");
  });

  it("submits a plain directory login name with no @ in the email field", async () => {
    let body: unknown = null;
    server.use(
      http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
      http.post("/api/auth/login", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ access_token: "a", refresh_token: "r" });
      }),
    );
    renderWithProviders(<LoginPage />);
    const id = (await screen.findByLabelText("Email")) as HTMLInputElement;
    fireEvent.change(id, { target: { value: "ldapit-eng1" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    // The browser's own constraint validation is what blocked the form before.
    expect(id.checkValidity()).toBe(true);
    expect(id.closest("form")!.checkValidity()).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(body).toEqual({ email: "ldapit-eng1", password: "pw" }));
  });

  it("names the icon-only configuration link", () => {
    renderWithProviders(<LoginPage />);
    expect(screen.getByRole("link", { name: "System configuration" })).toHaveAttribute(
      "href",
      "/config",
    );
  });
});

// Issue #1333: only a 401 from the login route is a wrong credential.
describe("LoginPage failure messages (issue #1333)", () => {
  async function submitAndGetToast(loginHandler: Parameters<typeof http.post>[1]) {
    server.use(
      http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
      http.post("/api/auth/login", loginHandler),
    );
    renderWithProviders(<LoginPage />);
    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));
    expect(mockNavigate).not.toHaveBeenCalled();
    return toastError.mock.calls[0][0] as string;
  }

  const UNREACHABLE = "Could not reach HERD. Try again in a moment.";

  it.each([502, 503, 504, 500, 404])(
    "a %i says HERD is unreachable, not that the password is wrong",
    async (status) => {
      const text = await submitAndGetToast(() =>
        HttpResponse.json({ detail: "upstream boom" }, { status }),
      );
      expect(text).toBe(UNREACHABLE);
    },
  );

  it("a network error says HERD is unreachable", async () => {
    const text = await submitAndGetToast(() => HttpResponse.error());
    expect(text).toBe(UNREACHABLE);
  });

  it("a 422 names the length limits instead of the credentials", async () => {
    const text = await submitAndGetToast(() =>
      HttpResponse.json({ detail: [{ msg: "too long" }] }, { status: 422 }),
    );
    expect(text).toBe(
      "HERD could not accept these sign-in details. The email or username can be at most 255 characters, and the password at most 72 characters, or 1024 for a directory account.",
    );
  });

  it("a 429 says to wait, with the Retry-After seconds", async () => {
    const text = await submitAndGetToast(() =>
      HttpResponse.json({ detail: "slow down" }, { status: 429, headers: { "Retry-After": "30" } }),
    );
    expect(text).toBe("Too many sign-in attempts. Try again in 30 seconds.");
  });

  it("a login that never answers is given up after 15 seconds and says HERD is unreachable", async () => {
    server.use(
      http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
      http.post("/api/auth/login", () => new Promise<never>(() => {})),
    );
    renderWithProviders(<LoginPage />);
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "u@test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
      await vi.advanceTimersByTimeAsync(14_999);
      expect(toastError).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(UNREACHABLE));
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("a 403 gets the unexpected-answer wording, never the server's text", async () => {
    const text = await submitAndGetToast(() =>
      HttpResponse.json({ detail: "forbidden by proxy" }, { status: 403 }),
    );
    expect(text).toBe("Sign-in failed with an unexpected answer. Try again in a moment.");
  });
});

describe("LoginPage return path (issue #1333)", () => {
  async function renderAt(state: unknown, accessToken = "a") {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    server.use(
      http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
      http.post("/api/auth/login", () =>
        HttpResponse.json({ access_token: accessToken, refresh_token: "r" }),
      ),
    );
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[{ pathname: "/login", state }]}>
          <LoginPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  const renderAtAs = renderAt;

  it("returns the same account to the page recorded for it", async () => {
    await renderAtAs({ from: "/reservations/alice-res-1", sub: "alice" }, fakeJwt("alice"));
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/reservations/alice-res-1", { replace: true }),
    );
  });

  it("sends another account to /topology, never to the page recorded for the previous one", async () => {
    await renderAtAs({ from: "/reservations/alice-res-1", sub: "alice" }, fakeJwt("bob"));
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/topology", { replace: true }),
    );
    expect(mockNavigate).not.toHaveBeenCalledWith("/reservations/alice-res-1", expect.anything());
  });

  it("returns to the recorded page, query and hash included", async () => {
    await renderAt({ from: "/topology/abc?tab=wiring#n1" });
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/topology/abc?tab=wiring#n1", { replace: true }),
    );
  });

  it.each(["//evil.example/x", "https://evil.example", "/\\evil.example"])(
    "refuses the unsafe return path %s and goes to /topology",
    async (from) => {
      await renderAt({ from });
      await waitFor(() =>
        expect(mockNavigate).toHaveBeenCalledWith("/topology", { replace: true }),
      );
    },
  );
});

// The author credit sits under the sign-in card in every sign-in state, after
// every sign-in control in tab order, so it never takes the first Tab press.
describe("LoginPage author credit", () => {
  const OIDC = { auth_method: "oidc", registration_open: false, sso: { provider_name: "Acme SSO" } };

  function answerLoginOptions(body: object) {
    server.use(http.get("/api/auth/login-options", () => HttpResponse.json(body)));
  }

  it("renders under the card in local mode and follows every form control in tab order", async () => {
    renderWithProviders(<LoginPage />);
    const email = await screen.findByLabelText("Email");
    const link = expectAuthorCredit();
    const order = await tabToAuthorCredit();
    expect(order[0]).toBe(email);
    expect(order[order.length - 1]).toBe(link);
    const before = order.slice(0, -1);
    for (const control of [
      email,
      screen.getByLabelText("Password"),
      screen.getByRole("button", { name: "Sign in" }),
      screen.getByRole("link", { name: "Register" }),
      screen.getByRole("link", { name: "System configuration" }),
    ]) {
      expect(before).toContain(control);
    }
    // Outside the card's form, in a footer stacked under the card.
    expect(link.closest("form")).toBeNull();
    expectCreditStackedUnderCard(link, "Hardware Environment Replication and Deployment");
  });

  it("renders while the sign-in options are pending, after the configuration link", async () => {
    server.use(http.get("/api/auth/login-options", () => new Promise<never>(() => {}) as never));
    renderWithProviders(<LoginPage />);
    const link = expectAuthorCredit();
    const order = await tabToAuthorCredit();
    expect(order).toEqual([screen.getByRole("link", { name: "System configuration" }), link]);
  });

  it("renders with single sign-on only, after the Sign in with button", async () => {
    answerLoginOptions({ ...OIDC, password_login: false });
    renderWithProviders(<LoginPage />);
    const sso = await screen.findByRole("button", { name: "Sign in with Acme SSO" });
    const link = expectAuthorCredit();
    const order = await tabToAuthorCredit();
    expect(order[0]).toBe(sso);
    expect(order[order.length - 1]).toBe(link);
  });

  it("renders with the Administrator sign-in toggle, after the opened form's submit", async () => {
    answerLoginOptions({ ...OIDC, password_login: true });
    renderWithProviders(<LoginPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Administrator sign-in" }));
    const link = expectAuthorCredit();
    const submit = screen.getByRole("button", { name: "Sign in" });
    const order = await tabToAuthorCredit();
    expect(order).toContain(submit);
    expect(order.indexOf(submit)).toBeLessThan(order.indexOf(link));
  });

  it("renders with the LDAP answer, whose form is the local one", async () => {
    answerLoginOptions({ auth_method: "ldap", registration_open: false, password_login: true, sso: null });
    renderWithProviders(<LoginPage />);
    await screen.findByLabelText("Email");
    expectAuthorCredit();
    expect(screen.queryByRole("link", { name: "Register" })).toBeNull();
  });
});
