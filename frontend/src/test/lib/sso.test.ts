import { describe, it, expect } from "vitest";
import { safeReturnPath } from "@/lib/login";
import {
  DEFAULT_SSO_PROVIDER_NAME,
  LEGACY_LOGIN_VIEW,
  SSO_ERROR_CANCELLED,
  SSO_ERROR_FAILED,
  SSO_ERROR_UNAVAILABLE,
  SSO_COMPLETE_PATH,
  SSO_EXCHANGE_FAILED_PATH,
  SSO_LOGIN_URL,
  loginPageView,
  parseLoginOptions,
  parseOidcExchange,
  ssoErrorMessage,
} from "@/lib/sso";

// ADR 0016 phase 4: the login page is a pure function of the
// GET /api/auth/login-options answer, and today's page on anything else.

describe("the fixed single sign-on strings (ADR 0016)", () => {
  it("are exactly the ADR's wording", () => {
    expect(SSO_ERROR_CANCELLED).toBe("Sign-in was cancelled.");
    expect(SSO_ERROR_UNAVAILABLE).toBe("Single sign-on is unavailable right now. Try again later.");
    expect(SSO_ERROR_FAILED).toBe(
      "Single sign-on failed. If this continues, contact an administrator.",
    );
    expect(SSO_LOGIN_URL).toBe("/api/auth/oidc/login");
    expect(SSO_EXCHANGE_FAILED_PATH).toBe("/login?sso_error=failed");
    expect(DEFAULT_SSO_PROVIDER_NAME).toBe("single sign-on");
    expect(SSO_COMPLETE_PATH).toBe("/login/sso");
  });

  it("the completion page is never a return path (it sits under GuestGuard)", () => {
    expect(safeReturnPath(SSO_COMPLETE_PATH)).toBeNull();
  });
});

describe("ssoErrorMessage", () => {
  it.each([
    ["cancelled", SSO_ERROR_CANCELLED],
    ["unavailable", SSO_ERROR_UNAVAILABLE],
    ["failed", SSO_ERROR_FAILED],
  ])("maps %s to its own message", (value, message) => {
    expect(ssoErrorMessage(value)).toBe(message);
  });

  it.each([
    "",
    "Cancelled",
    "CANCELLED",
    " cancelled",
    "unavailable ",
    "access_denied",
    "<img src=x onerror=alert(1)>",
    "Your account is locked, call 555-0100",
  ])("maps any other value, %j included, to the failed message and never echoes it", (value) => {
    expect(ssoErrorMessage(value)).toBe(SSO_ERROR_FAILED);
  });
});

// Every answer the backend can produce (the per-mode table, IAM-OIDC-16) and
// every other combination of the three fields that drive rendering.
describe("parseLoginOptions and loginPageView: the full input space", () => {
  const ssoValues: Array<[string, unknown]> = [
    ["sso null", null],
    ["sso absent", undefined],
    ["sso with a name", { provider_name: "Okta" }],
  ];
  const cases: Array<[string, boolean, boolean, unknown]> = [];
  for (const [ssoLabel, sso] of ssoValues) {
    for (const passwordLogin of [true, false]) {
      for (const registrationOpen of [true, false]) {
        cases.push([ssoLabel, passwordLogin, registrationOpen, sso]);
      }
    }
  }

  it.each(cases)(
    "%s, password_login %s, registration_open %s",
    (_label, passwordLogin, registrationOpen, sso) => {
      const body: Record<string, unknown> = {
        auth_method: "local",
        password_login: passwordLogin,
        registration_open: registrationOpen,
      };
      if (sso !== undefined) body.sso = sso;
      const parsed = parseLoginOptions(body);
      const ssoPresent = sso !== null && sso !== undefined;
      if (!ssoPresent && !passwordLogin) {
        // No way to sign in: never a backend answer, so today's page.
        expect(parsed).toBeNull();
        expect(loginPageView(parsed)).toEqual(LEGACY_LOGIN_VIEW);
        return;
      }
      const view = loginPageView(parsed);
      expect(view.ssoLabel).toBe(ssoPresent ? "Sign in with Okta" : null);
      expect(view.passwordForm).toBe(!passwordLogin ? "none" : ssoPresent ? "toggle" : "shown");
      expect(view.registerLink).toBe(registrationOpen);
    },
  );

  it.each([
    [
      "local",
      { auth_method: "local", registration_open: true, password_login: true, sso: null },
      { ssoLabel: null, passwordForm: "shown", registerLink: true },
    ],
    [
      "ldap",
      { auth_method: "ldap", registration_open: false, password_login: true, sso: null },
      { ssoLabel: null, passwordForm: "shown", registerLink: false },
    ],
    [
      "oidc, break-glass off",
      {
        auth_method: "oidc",
        registration_open: false,
        password_login: false,
        sso: { provider_name: "Acme SSO" },
      },
      { ssoLabel: "Sign in with Acme SSO", passwordForm: "none", registerLink: false },
    ],
    [
      "oidc, break-glass on",
      {
        auth_method: "oidc",
        registration_open: false,
        password_login: true,
        sso: { provider_name: "Acme SSO" },
      },
      { ssoLabel: "Sign in with Acme SSO", passwordForm: "toggle", registerLink: false },
    ],
  ])("the backend's %s answer", (_mode, body, view) => {
    expect(loginPageView(parseLoginOptions(body))).toEqual(view);
  });

  it("local mode renders exactly today's page", () => {
    const local = { auth_method: "local", registration_open: true, password_login: true, sso: null };
    expect(loginPageView(parseLoginOptions(local))).toEqual(LEGACY_LOGIN_VIEW);
    expect(LEGACY_LOGIN_VIEW).toEqual({ ssoLabel: null, passwordForm: "shown", registerLink: true });
  });

  it("no answer yet, or an error, is today's page", () => {
    expect(loginPageView(null)).toEqual(LEGACY_LOGIN_VIEW);
  });

  const NOT_AN_ANSWER: Array<[string, unknown]> = [
    ["null", null],
    ["a string (an HTML error page)", "<html>502 Bad Gateway</html>"],
    ["a number", 42],
    ["an array", [{ password_login: true, registration_open: true }]],
    ["an empty object", {}],
    ["password_login missing", { registration_open: true, sso: null }],
    ["registration_open missing", { password_login: true, sso: null }],
    ["password_login a string", { password_login: "true", registration_open: true, sso: null }],
    ["registration_open a number", { password_login: true, registration_open: 1, sso: null }],
    ["password_login null", { password_login: null, registration_open: true, sso: null }],
    ["sso a string", { password_login: true, registration_open: true, sso: "Okta" }],
    ["sso an array", { password_login: true, registration_open: true, sso: [] }],
    ["sso true", { password_login: true, registration_open: true, sso: true }],
    ["no sign-in method at all", { password_login: false, registration_open: true, sso: null }],
  ];
  it.each(NOT_AN_ANSWER)("refuses %s as an answer, so the page is today's", (_label, body) => {
    expect(parseLoginOptions(body)).toBeNull();
    expect(loginPageView(parseLoginOptions(body))).toEqual(LEGACY_LOGIN_VIEW);
  });

  it.each<[string, unknown]>([
    ["absent", {}],
    ["null", { provider_name: null }],
    ["a number", { provider_name: 7 }],
    ["empty", { provider_name: "" }],
    ["blank", { provider_name: "   " }],
  ])("an sso object whose provider_name is %s still shows the button, with the default name", (_l, sso) => {
    const parsed = parseLoginOptions({ password_login: false, registration_open: false, sso });
    expect(parsed?.sso).toEqual({ provider_name: DEFAULT_SSO_PROVIDER_NAME });
    expect(loginPageView(parsed).ssoLabel).toBe("Sign in with single sign-on");
  });

  it("auth_method is informational: an unknown or missing value changes nothing rendered", () => {
    const base = { registration_open: false, password_login: true, sso: null };
    const known = parseLoginOptions({ ...base, auth_method: "ldap" });
    const unknown = parseLoginOptions({ ...base, auth_method: "saml" });
    const missing = parseLoginOptions(base);
    expect(known?.auth_method).toBe("ldap");
    expect(unknown?.auth_method).toBeNull();
    expect(missing?.auth_method).toBeNull();
    expect(loginPageView(unknown)).toEqual(loginPageView(known));
    expect(loginPageView(missing)).toEqual(loginPageView(known));
  });
});

describe("parseOidcExchange", () => {
  const tokens = { access_token: "A", refresh_token: "R", token_type: "bearer" };

  it("keeps the tokens and a safe in-app next", () => {
    expect(parseOidcExchange({ ...tokens, next: "/inventory?x=1#y" })).toEqual({
      access_token: "A",
      refresh_token: "R",
      next: "/inventory?x=1#y",
    });
  });

  const HOSTILE_NEXT: Array<[string, unknown]> = [
    ["an absolute https URL", "https://evil.example/topology"],
    ["a protocol-relative URL", "//evil.example"],
    ["a protocol-relative URL with a path", "//evil.example/topology"],
    ["a backslash after the slash", "/\\evil.example"],
    ["a backslash pair", "\\\\evil.example"],
    ["a javascript: URL", "javascript:alert(document.cookie)"],
    ["a data: URL", "data:text/html,<script>alert(1)</script>"],
    ["a relative path", "topology"],
    ["a tab smuggling a second slash", "/\t/evil.example"],
    ["a newline", "/topology\n//evil.example"],
    ["the empty string", ""],
    ["the login page", "/login"],
    ["the register page", "/register"],
    ["the completion page itself", "/login/sso"],
    ["absent", undefined],
    ["null", null],
    ["a number", 7],
    ["an object", { pathname: "/inventory" }],
  ];
  it.each(HOSTILE_NEXT)("replaces %s with /topology", (_label, next) => {
    expect(parseOidcExchange({ ...tokens, next })?.next).toBe("/topology");
  });

  const NO_TOKEN_PAIR: Array<[string, unknown]> = [
    ["null", null],
    ["a string", "ok"],
    ["an array", [tokens]],
    ["access_token missing", { refresh_token: "R", next: "/topology" }],
    ["refresh_token missing", { access_token: "A", next: "/topology" }],
    ["access_token empty", { access_token: "", refresh_token: "R", next: "/topology" }],
    ["refresh_token empty", { access_token: "A", refresh_token: "", next: "/topology" }],
    ["access_token not a string", { access_token: 1, refresh_token: "R", next: "/topology" }],
    ["refresh_token not a string", { access_token: "A", refresh_token: {}, next: "/topology" }],
  ];
  it.each(NO_TOKEN_PAIR)("refuses an answer with %s", (_label, body) => {
    expect(parseOidcExchange(body)).toBeNull();
  });
});
