import type { LoginOptions } from "@/types/auth.types";
import { DEFAULT_AFTER_LOGIN, safeReturnPath } from "@/lib/login";

/**
 * Single sign-on in the browser (ADR 0016 phase 4). Everything here is pure
 * except `SSO_LOGIN_URL`, which the login page hands to a top-level
 * navigation.
 */

/**
 * Where the "Sign in with" button sends the browser: auth's begin route, as a
 * top-level navigation, never an XHR. A fixed same-origin path with no query:
 * the frontend never builds the provider's URL and sends no `next` in v1 (ADR
 * 0016, Frontend contract), so the backend's default `/topology` applies.
 */
export const SSO_LOGIN_URL = "/api/auth/oidc/login";

/** Label used when `sso.provider_name` is not a usable string (the backend's default). */
export const DEFAULT_SSO_PROVIDER_NAME = "single sign-on";

export const SSO_ERROR_CANCELLED = "Sign-in was cancelled.";
export const SSO_ERROR_UNAVAILABLE = "Single sign-on is unavailable right now. Try again later.";
export const SSO_ERROR_FAILED = "Single sign-on failed. If this continues, contact an administrator.";

/** The query parameter auth's single sign-on routes redirect to `/login` with. */
export const SSO_ERROR_PARAM = "sso_error";

/** The single sign-on completion page, where auth's callback sends the browser. */
export const SSO_COMPLETE_PATH = "/login/sso";

/** Where a failed exchange sends the browser: the login page's `failed` message. */
export const SSO_EXCHANGE_FAILED_PATH = `/login?${SSO_ERROR_PARAM}=failed`;

/**
 * The fixed message for an `sso_error` value (ADR 0016): `cancelled` and
 * `unavailable` have their own; `failed` and ANY other value, the empty
 * string included, get the `failed` message. The raw value is never shown.
 */
export function ssoErrorMessage(value: string): string {
  if (value === "cancelled") return SSO_ERROR_CANCELLED;
  if (value === "unavailable") return SSO_ERROR_UNAVAILABLE;
  return SSO_ERROR_FAILED;
}

/**
 * What the login page renders, decided by the `GET /api/auth/login-options`
 * answer alone:
 * - `ssoLabel`: the "Sign in with" button's label, or null for no button;
 * - `passwordForm`: `"shown"` (today's form), `"toggle"` (behind the
 *   "Administrator sign-in" disclosure, single sign-on mode), or `"none"`;
 * - `registerLink`: whether the Register link renders.
 */
export interface LoginPageView {
  ssoLabel: string | null;
  passwordForm: "shown" | "toggle" | "none";
  registerLink: boolean;
}

/**
 * Today's page (ADR 0016 D10): what renders while the answer is pending and on
 * ANY error (no answer, a 404 from a backend without the route, a 5xx, a body
 * that is not a valid answer). The backend enforces the mode regardless, so
 * this fallback widens nothing.
 */
export const LEGACY_LOGIN_VIEW: LoginPageView = {
  ssoLabel: null,
  passwordForm: "shown",
  registerLink: true,
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The login options a `GET /login-options` body states, or null when the body
 * is not a valid answer. Valid means: a JSON object whose `password_login` and
 * `registration_open` are booleans and whose `sso` is null, absent, or an
 * object (not an array), and which offers at least one way to sign in (an
 * `sso` object or `password_login` true; the backend never answers with
 * neither, ADR 0016 per-mode table). `auth_method` is informational and not
 * read for rendering. `provider_name` is kept only when it is a string with a
 * non-blank character; otherwise the button uses DEFAULT_SSO_PROVIDER_NAME.
 */
export function parseLoginOptions(body: unknown): LoginOptions | null {
  if (!isPlainObject(body)) return null;
  const { password_login: passwordLogin, registration_open: registrationOpen, sso } = body;
  if (typeof passwordLogin !== "boolean" || typeof registrationOpen !== "boolean") return null;
  if (sso !== null && sso !== undefined && !isPlainObject(sso)) return null;
  const ssoOptions = isPlainObject(sso)
    ? {
        provider_name:
          typeof sso.provider_name === "string" && sso.provider_name.trim() !== ""
            ? sso.provider_name
            : DEFAULT_SSO_PROVIDER_NAME,
      }
    : null;
  if (!ssoOptions && !passwordLogin) return null;
  const method = body.auth_method;
  return {
    auth_method: method === "local" || method === "ldap" || method === "oidc" ? method : null,
    registration_open: registrationOpen,
    password_login: passwordLogin,
    sso: ssoOptions,
  };
}

/**
 * The login page as a pure function of the parsed answer (null: pending or
 * any error, today's page). The SSO button renders exactly when `sso` is
 * present; the password form exactly when `password_login` is true, behind
 * the toggle when `sso` is present too; the Register link exactly when
 * `registration_open` is true.
 */
export function loginPageView(options: LoginOptions | null): LoginPageView {
  if (!options) return LEGACY_LOGIN_VIEW;
  const ssoLabel = options.sso ? `Sign in with ${options.sso.provider_name}` : null;
  let passwordForm: LoginPageView["passwordForm"] = "none";
  if (options.password_login) passwordForm = ssoLabel ? "toggle" : "shown";
  return { ssoLabel, passwordForm, registerLink: options.registration_open };
}

/** A successful `POST /api/auth/oidc/exchange`, validated. */
export interface OidcExchangeResult {
  access_token: string;
  refresh_token: string;
  /** Always a safe in-app path: the answered `next` when safeReturnPath honors it, else `/topology`. */
  next: string;
}

/**
 * The exchange answer, or null when it carries no usable token pair (either
 * token missing, not a string, or empty). `next` is honored only through
 * safeReturnPath (one leading `/`, no backslash or control character, same
 * origin, not a sign-in page); anything else, absent included, becomes
 * `/topology`, so the page never navigates outside the app.
 */
export function parseOidcExchange(body: unknown): OidcExchangeResult | null {
  if (!isPlainObject(body)) return null;
  const { access_token: access, refresh_token: refresh, next } = body;
  if (typeof access !== "string" || access === "") return null;
  if (typeof refresh !== "string" || refresh === "") return null;
  return {
    access_token: access,
    refresh_token: refresh,
    next: safeReturnPath(next) ?? DEFAULT_AFTER_LOGIN,
  };
}
