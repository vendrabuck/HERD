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
});

describe("LoginPage", () => {
  it("renders the identifier, password, and submit controls", () => {
    renderWithProviders(<LoginPage />);
    expect(screen.getByLabelText("Email or username")).toBeInTheDocument();
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
    expect(screen.getByLabelText("Email or username")).toBeDisabled();
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
    fireEvent.change(screen.getByLabelText("Email or username"), {
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
    fireEvent.change(screen.getByLabelText("Email or username"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Invalid credentials. Check the email or username and the password.",
      ),
    );
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

// Issue #1335: in LDAP mode the identifier is the directory login name
// (IAM-LDAP-2), which need not contain an @, and the page cannot yet tell the
// mode, so the field must accept any text in both modes.
describe("LoginPage identifier field (issue #1335)", () => {
  it("is a text field with username autocomplete and an email keyboard, never type=email", () => {
    renderWithProviders(<LoginPage />);
    const id = screen.getByRole("textbox", { name: "Email or username" }) as HTMLInputElement;
    expect(id.type).toBe("text");
    expect(id.getAttribute("autocomplete")).toBe("username");
    expect(id.getAttribute("inputmode")).toBe("email");
    expect(id.placeholder).toBe("Email or username");
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
    const id = screen.getByLabelText("Email or username") as HTMLInputElement;
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
    fireEvent.change(screen.getByLabelText("Email or username"), {
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
      "HERD could not accept these sign-in details. The email or username can be at most 255 characters and the password at most 72.",
    );
  });

  it("a 429 says to wait, with the Retry-After seconds", async () => {
    const text = await submitAndGetToast(() =>
      HttpResponse.json({ detail: "slow down" }, { status: 429, headers: { "Retry-After": "30" } }),
    );
    expect(text).toBe("Too many sign-in attempts. Try again in 30 seconds.");
  });

  it("a 403 gets the unexpected-answer wording, never the server's text", async () => {
    const text = await submitAndGetToast(() =>
      HttpResponse.json({ detail: "forbidden by proxy" }, { status: 403 }),
    );
    expect(text).toBe("Sign-in failed with an unexpected answer. Try again in a moment.");
  });
});

describe("LoginPage return path (issue #1333)", () => {
  function renderAt(state: unknown) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    server.use(
      http.get("/api/config/status", () => HttpResponse.json({ configured: true })),
      http.post("/api/auth/login", () =>
        HttpResponse.json({ access_token: "a", refresh_token: "r" }),
      ),
    );
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[{ pathname: "/login", state }]}>
          <LoginPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText("Email or username"), {
      target: { value: "u@test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  it("returns to the recorded page, query and hash included", async () => {
    renderAt({ from: "/topology/abc?tab=wiring#n1" });
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/topology/abc?tab=wiring#n1", { replace: true }),
    );
  });

  it.each(["//evil.example/x", "https://evil.example", "/\\evil.example"])(
    "refuses the unsafe return path %s and goes to /topology",
    async (from) => {
      renderAt({ from });
      await waitFor(() =>
        expect(mockNavigate).toHaveBeenCalledWith("/topology", { replace: true }),
      );
    },
  );
});
